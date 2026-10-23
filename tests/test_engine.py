import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mt5_mcp.broker import BrokerError, MT5Broker
from mt5_mcp.config import RiskConfig, load_config
from mt5_mcp.engine import Engine
from mt5_mcp.journal import Journal


def make_engine(fake, tmp_path, **cfg_changes) -> Engine:
    base = dict(symbols=("EURUSD", "GBPUSD"), max_open_positions=2, max_volume_per_order=0.5, max_total_volume=1.0)
    base.update(cfg_changes)
    return Engine(MT5Broker(module=fake), cfg=RiskConfig(**base), root=tmp_path, journal=Journal(tmp_path / "journal" / "trades.jsonl"))


# ---- lecturas ---------------------------------------------------------------------------------


def test_account_info_and_risk_status(engine, fake):
    info = engine.account_info()
    assert info["mode"] == "DEMO" and info["is_demo"] and info["algo_trading_enabled"] and not info["halted"]
    assert info["python_api_trading_disabled"] is False and info["account_trade_allowed"] and info["account_expert_allowed"]
    rs = engine.risk_status()
    assert rs["can_open_new_positions"] and rs["blockers"] == [] and rs["daily_loss_limit"] == -300.0 and rs["pnl_today"] == 0


def test_risk_status_counts_today_including_fee_and_ignoring_balance_ops(engine, fake):
    fake.add_deal(profit=-120.0, commission=-2.0, fee=-1.0)
    fake.add_deal(profit=5_000.0, deal_type=2)  # depósito: no es P&L y no amplía el límite
    fake.set_account(balance=14_877.0, equity=14_862.0)
    fake.add_position("EURUSD", "buy", 0.1, 1.1000, sl=1.0980, profit=-15.0)
    rs = engine.risk_status()
    assert rs["realized_today"] == -123.0 and rs["floating_pnl"] == -15.0 and rs["open_positions"] == 1
    assert rs["balance_ops_today"] == 5_000.0 and rs["daily_loss_limit"] == -300.0
    assert rs["remaining_before_daily_halt"] == 162.0
    assert engine.history_today()["realized_pnl"] == -123.0 and engine.history_today()["balance_ops"] == 5_000.0


def test_quote_and_symbol_info(engine, fake):
    q = engine.quote("eurusd")
    assert q["symbol"] == "EURUSD" and q["bid"] == 1.1 and q["ask"] == 1.1001 and q["spread_points"] == 10 and q["stale"] is False
    fake.set_tick("EURUSD", 1.1, 1.1001, time=int(time.time()) - 3 * 3600)
    assert engine.quote("EURUSD")["stale"] is True
    s = engine.symbol_info("EURUSD")
    assert s["allowed_by_config"] and s["filling_chosen"] == 0


def test_symbol_with_lowercase_suffix_is_resolved_exactly(fake, tmp_path):
    fake.add_symbol("EURUSD.m")
    fake.set_tick("EURUSD.m", 1.2000, 1.2001)
    eng = make_engine(fake, tmp_path, symbols=("eurusd.m",))
    assert eng.quote("eurusd.M")["symbol"] == "EURUSD.m"
    assert eng.symbol_info("EURUSD.m")["allowed_by_config"] is True
    r = eng.order_check("EURUSD.m", "buy", sl=1.1981)
    assert r["would_send"] and r["request"]["symbol"] == "EURUSD.m"
    with pytest.raises(BrokerError, match="no existe"):
        eng.quote("NOPE")


def test_symbol_not_in_market_watch_is_selected(engine, fake):
    fake.add_symbol("GBPUSD", visible=False)
    fake.set_tick("GBPUSD", 1.3000, 1.3002)
    assert engine.quote("GBPUSD")["bid"] == 1.3
    assert fake.symbols["GBPUSD"].visible is True


def test_deals_window_is_utc_midnight_to_now(engine, fake, monkeypatch):
    captured = {}
    orig = fake.history_deals_get

    def spy(date_from, date_to):
        captured["from"], captured["to"] = date_from, date_to
        return orig(date_from, date_to)

    monkeypatch.setattr(fake, "history_deals_get", spy)
    now = datetime(2026, 10, 23, 15, 30, 45, tzinfo=timezone.utc)
    monkeypatch.setattr(engine, "_now", lambda: now)
    fake.add_deal(profit=-50.0, time=int(datetime(2026, 10, 23, 1, 0, tzinfo=timezone.utc).timestamp()))
    fake.add_deal(profit=-999.0, time=int(datetime(2026, 10, 22, 23, 59, tzinfo=timezone.utc).timestamp()))  # ayer
    assert engine.history_today()["realized_pnl"] == -50.0
    assert captured["from"] == datetime(2026, 10, 23, tzinfo=timezone.utc) and captured["to"] > now


def test_position_size(engine):
    r = engine.position_size("EURUSD", "buy", sl=1.0981)
    assert r["volume"] == 0.5 and r["risk_budget"] == 100.0 and r["actual_risk"] == 100.0
    assert engine.position_size("EURUSD", "buy", sl=1.0981, risk_pct=0.5)["volume"] == 0.25
    assert "error" in engine.position_size("EURUSD", "buy", sl=1.2)
    assert "error" in engine.position_size("EURUSD", "buy", sl=float("nan"))


def test_order_check_does_not_send_and_reports_broker_rejection(engine, fake):
    r = engine.order_check("EURUSD", "buy", sl=1.0981, tp=1.1041)
    assert r["would_send"] and r["request"]["volume"] == 0.5 and r["request"]["sl"] == 1.0981
    assert fake.checked and not fake.sent
    assert engine.order_check("EURUSD", "buy", sl=1.0981, risk_pct=0.5)["summary"]["volume"] == 0.25
    fake.check_retcode = 10019
    r2 = engine.order_check("EURUSD", "buy", sl=1.0981)
    assert r2["would_send"] is False and r2["broker_check"]["retcode_name"] == "NO_MONEY" and r2["evaluation"]["ok"] and not fake.sent


# ---- fail closed ante errores del terminal ----------------------------------------------------


def test_positions_error_blocks_everything(engine, fake):
    fake.add_position("EURUSD", "buy", 0.1, 1.0990, sl=1.0970)
    fake.fail_positions = True
    with pytest.raises(BrokerError, match="positions_get"):
        engine.risk_status()
    with pytest.raises(BrokerError):
        engine.order_send("EURUSD", "buy", sl=1.0981, confirm=True)
    assert not fake.checked and not fake.sent


def test_history_error_blocks_everything(engine, fake):
    fake.fail_history = True
    with pytest.raises(BrokerError, match="history_deals_get"):
        engine.risk_status()
    with pytest.raises(BrokerError):
        engine.order_send("EURUSD", "buy", sl=1.0981, confirm=True)
    assert not fake.sent


def test_orders_error_is_an_error(engine, fake):
    fake.fail_orders = True
    with pytest.raises(BrokerError, match="orders_get"):
        engine.pending_orders()


# ---- order_send -------------------------------------------------------------------------------


def test_order_send_requires_confirm(engine, fake):
    r = engine.order_send("EURUSD", "buy", sl=1.0981)
    assert not r["sent"] and r["status"] == "needs_confirmation" and "confirm=true" in r["error"] and not fake.sent
    assert engine.journal_tail()[-1]["event"] == "order_pending_confirmation"


def test_order_send_happy_path_builds_request(engine, fake, cfg):
    r = engine.order_send("EURUSD", "buy", sl=1.0981, tp=1.1041, comment="setup A", confirm=True)
    assert r["sent"] and r["status"] == "filled" and r["retcode_name"] == "DONE" and r["order"] == 777 and r["partial"] is False
    req = fake.sent[-1]
    assert req["action"] == fake.TRADE_ACTION_DEAL and req["type"] == fake.ORDER_TYPE_BUY
    assert req["price"] == 1.1001 and req["volume"] == 0.5 and req["sl"] == 1.0981 and req["tp"] == 1.1041
    assert req["magic"] == cfg.magic and req["deviation"] == cfg.max_slippage_points and req["type_filling"] == fake.ORDER_FILLING_FOK
    assert req["comment"] == "setup A" and req["type_time"] == fake.ORDER_TIME_GTC
    assert fake.checked[-1] == req
    assert engine.positions()[0]["ticket"] == 1000
    assert engine.journal_tail()[-1]["event"] == "order_sent"


def test_order_send_with_explicit_volume_from_summary(engine, fake):
    r = engine.order_send("EURUSD", "buy", sl=1.0981, volume=0.3, confirm=True)
    assert r["sent"] and fake.sent[-1]["volume"] == 0.3 and r["summary"]["risk_amount"] == 60.0


def test_order_send_sell_uses_bid(engine, fake):
    r = engine.order_send("EURUSD", "sell", sl=1.1020, confirm=True)
    assert r["sent"] and fake.sent[-1]["price"] == 1.1 and fake.sent[-1]["type"] == fake.ORDER_TYPE_SELL


def test_order_send_normalizes_prices_to_tick_grid(fake, tmp_path):
    fake.add_symbol("US500", digits=2, point=0.01, tick_size=0.25, tick_value=0.25, volume_min=0.1, volume_step=0.1, filling_mode=2)
    fake.set_tick("US500", 5000.00, 5000.50)
    eng = make_engine(fake, tmp_path, symbols=("US500",), max_volume_per_order=1.0, max_total_volume=2.0)
    r = eng.order_send("US500", "buy", sl=4990.30, tp=5010.40, confirm=True)
    assert r["sent"] and fake.sent[-1]["sl"] == 4990.25 and fake.sent[-1]["tp"] == 5010.5 and fake.sent[-1]["type_filling"] == fake.ORDER_FILLING_IOC


def test_order_send_rejected_by_risk_never_reaches_broker(engine, fake):
    fake.add_symbol("USDJPY", digits=3, point=0.001, tick_size=0.001, tick_value=0.65)
    fake.set_tick("USDJPY", 150.000, 150.010)
    r = engine.order_send("USDJPY", "buy", sl=149.500, confirm=True)  # USDJPY no está en la lista blanca del test
    assert not r["sent"] and r["status"] == "rejected" and not fake.sent and not fake.checked
    assert engine.journal_tail()[-1]["event"] == "order_rejected_by_risk"


def test_order_send_live_account_blocked(fake, tmp_path):
    fake.set_account(trade_mode=2)
    eng = make_engine(fake, tmp_path)
    r = eng.order_send("EURUSD", "buy", sl=1.0981, confirm=True)
    assert not r["sent"] and any("REAL" in x for x in r["evaluation"]["reasons"]) and not fake.sent


def test_order_send_enforces_max_total_volume_with_open_positions(engine, fake):
    fake.add_position("EURUSD", "buy", 0.6, 1.0990, sl=1.0970)
    assert engine.risk_status()["total_volume"] == 0.6
    r = engine.order_send("EURUSD", "buy", sl=1.0981, confirm=True)  # por riesgo 0.5 -> 1.1 > 1.0
    assert not r["sent"] and any("max_total_volume" in x for x in r["evaluation"]["reasons"]) and not fake.checked and not fake.sent


def test_order_send_rejected_outside_trading_hours_and_without_tp(fake, tmp_path, monkeypatch):
    eng = make_engine(fake, tmp_path, trading_hours_utc=(7, 20), require_tp=True)
    monkeypatch.setattr(eng, "_now", lambda: datetime(2026, 10, 23, 22, 30, tzinfo=timezone.utc))
    r = eng.order_send("EURUSD", "buy", sl=1.0981, confirm=True)
    reasons = r["evaluation"]["reasons"]
    assert not r["sent"] and any("fuera del horario" in x for x in reasons) and any("take profit obligatorio" in x for x in reasons)
    assert not fake.sent


def test_daily_loss_boundary_end_to_end(fake, tmp_path):
    fake.add_deal(profit=-300.0)
    fake.set_account(balance=9_700.0, equity=9_700.0)
    eng = make_engine(fake, tmp_path)
    rs = eng.risk_status()
    assert rs["daily_loss_hit"] and rs["can_open_new_positions"] is False and rs["remaining_before_daily_halt"] == 0.0
    r = eng.order_send("EURUSD", "buy", sl=1.0981, confirm=True)
    assert not r["sent"] and any("pérdida diaria" in x for x in r["evaluation"]["reasons"]) and not fake.sent


def test_autotrading_off_and_python_api_disabled_block_orders(engine, fake):
    fake.set_terminal(trade_allowed=False)
    rs = engine.risk_status()
    assert rs["algo_trading_enabled"] is False and rs["can_open_new_positions"] is False
    r = engine.order_send("EURUSD", "buy", sl=1.0981, confirm=True)
    assert not r["sent"] and any("AutoTrading" in x for x in r["evaluation"]["reasons"]) and not fake.sent
    fake.set_terminal(trade_allowed=True, tradeapi_disabled=True)
    assert engine.account_info()["python_api_trading_disabled"] is True
    r2 = engine.order_send("EURUSD", "buy", sl=1.0981, confirm=True)
    assert not r2["sent"] and any("API externa de Python" in x for x in r2["evaluation"]["reasons"]) and not fake.sent
    fake.set_terminal(tradeapi_disabled=False)
    fake.set_account(trade_expert=False)
    r3 = engine.order_send("EURUSD", "buy", sl=1.0981, confirm=True)
    assert not r3["sent"] and any("trade_expert" in x for x in r3["evaluation"]["reasons"])


def test_order_send_broker_check_failure(engine, fake):
    fake.check_retcode = 10019
    r = engine.order_send("EURUSD", "buy", sl=1.0981, confirm=True)
    assert not r["sent"] and "NO_MONEY" in r["error"] and not fake.sent


def test_order_send_broker_failure_reported(engine, fake):
    fake.send_retcode = 10004
    r = engine.order_send("EURUSD", "buy", sl=1.0981, confirm=True)
    assert not r["sent"] and r["status"] == "failed" and r["retcode_name"] == "REQUOTE" and "no reintentes" in r["next"]
    assert engine.journal_tail()[-1]["event"] == "order_failed"


def test_order_send_partial_fill_is_flagged(engine, fake):
    fake.send_retcode = 10010
    fake.send_volume = 0.2
    r = engine.order_send("EURUSD", "buy", sl=1.0981, confirm=True)
    assert r["sent"] and r["status"] == "partial" and r["partial"] and r["fill_volume"] == 0.2 and r["requested_volume"] == 0.5
    assert "NO envíes otra orden" in r["next"]


# ---- cierre, modificación, kill switch --------------------------------------------------------


def test_halt_blocks_new_orders_but_allows_close(engine, fake, tmp_path):
    t = fake.add_position("EURUSD", "buy", 0.1, 1.0990, sl=1.0970)
    res = engine.halt_trading(reason="prueba")
    assert res["halted"] and (tmp_path / "HALT").exists() and res["closed"] == [] and res["errors"] == []
    r = engine.order_send("EURUSD", "buy", sl=1.0981, confirm=True)
    assert not r["sent"] and any("kill switch" in x for x in r["evaluation"]["reasons"])
    c = engine.close_position(t, confirm=True)
    assert c["closed"] and c["status"] == "filled"
    assert fake.sent[-1]["position"] == t and fake.sent[-1]["type"] == fake.ORDER_TYPE_SELL and fake.sent[-1]["price"] == 1.1
    assert engine.positions() == []


def test_halt_is_latched_until_restart(engine, fake, tmp_path):
    engine.halt_trading(reason="parada")
    (tmp_path / "HALT").unlink()
    assert engine.halted() is True
    r = engine.order_send("EURUSD", "buy", sl=1.0981, confirm=True)
    assert not r["sent"] and not fake.sent
    fresh = make_engine(fake, tmp_path)  # "reinicio" del servidor sin archivo: se reanuda
    assert fresh.halted() is False


def test_halt_file_created_by_hand_is_detected(engine, fake, tmp_path):
    (tmp_path / "HALT").write_text("manual\n", encoding="utf-8")
    assert engine.risk_status()["halted"] is True


def test_halt_close_all_requires_confirm(engine, fake, tmp_path):
    fake.add_position("EURUSD", "buy", 0.1, 1.0990, sl=1.0970)
    res = engine.halt_trading(reason="cerrar todo", close_all=True)
    assert res["halted"] and res["closed"] == [] and res["errors"] and not fake.sent and (tmp_path / "HALT").exists()
    res2 = engine.halt_trading(reason="cerrar todo", close_all=True, confirm=True)
    assert len(res2["closed"]) == 1 and res2["closed"][0]["closed"] and res2["errors"] == [] and engine.positions() == []


def test_halt_close_all_reports_partial_and_failures(engine, fake):
    fake.add_position("EURUSD", "buy", 0.5, 1.0990, sl=1.0970)
    fake.add_position("EURUSD", "sell", 0.2, 1.1010, sl=1.1030)
    fake.send_retcode = 10010
    fake.send_volume = 0.1
    res = engine.halt_trading(reason="parcial", close_all=True, confirm=True)
    assert len(res["closed"]) == 2 and all(c["status"] == "partial" and not c["closed"] for c in res["closed"])
    assert len(res["errors"]) == 2 and len(engine.positions()) == 2


def test_close_requires_confirm_and_valid_ticket(engine, fake):
    t = fake.add_position("EURUSD", "buy", 0.1, 1.0990, sl=1.0970)
    assert not engine.close_position(t)["closed"] and not fake.sent
    assert "no existe" in engine.close_position(99)["error"]


def test_close_sell_position_sends_buy_at_ask_with_full_volume(engine, fake):
    t = fake.add_position("EURUSD", "sell", 0.2, 1.1010, sl=1.1030)
    r = engine.close_position(t, confirm=True)
    req = fake.sent[-1]
    assert r["closed"] and req["position"] == t and req["type"] == fake.ORDER_TYPE_BUY and req["price"] == 1.1001 and req["volume"] == 0.2
    assert engine.positions() == []


def test_close_partial_leaves_position_open(engine, fake):
    t = fake.add_position("EURUSD", "buy", 0.5, 1.0990, sl=1.0970)
    fake.send_retcode = 10010
    fake.send_volume = 0.2
    r = engine.close_position(t, confirm=True)
    assert r["closed"] is False and r["status"] == "partial" and r["partial"] and r["fill_volume"] == 0.2 and r["requested_volume"] == 0.5
    assert engine.positions()[0]["volume"] == 0.3
    assert engine.journal_tail()[-1]["event"] == "close_partial"


def test_close_reports_broker_rejection(engine, fake):
    t = fake.add_position("EURUSD", "buy", 0.1, 1.0990, sl=1.0970)
    fake.send_retcode = 10004
    r = engine.close_position(t, confirm=True)
    assert r["closed"] is False and r["status"] == "failed" and r["retcode_name"] == "REQUOTE"
    assert engine.positions()[0]["ticket"] == t and engine.journal_tail()[-1]["event"] == "close_failed"


def test_modify_position_rules_buy(engine, fake):
    t = fake.add_position("EURUSD", "buy", 0.1, 1.0990, sl=1.0970)
    assert "quitar el stop" in engine.modify_position(t, sl=0, confirm=True)["error"]
    assert "quitar el stop" in engine.modify_position(t, sl=float("nan"), confirm=True)["error"]
    assert "por debajo del bid" in engine.modify_position(t, sl=1.1050, confirm=True)["error"]
    assert not engine.modify_position(t, sl=1.0985)["modified"] and not fake.sent
    r = engine.modify_position(t, sl=1.0985, tp=1.1100, confirm=True)
    assert r["modified"] and fake.sent[-1]["action"] == fake.TRADE_ACTION_SLTP and fake.sent[-1]["sl"] == 1.0985 and fake.sent[-1]["tp"] == 1.11
    assert engine.positions()[0]["sl"] == 1.0985
    assert "no existe" in engine.modify_position(99, sl=1.0)["error"]


def test_modify_sell_position_validates_side_and_keeps_tp(engine, fake):
    t = fake.add_position("EURUSD", "sell", 0.1, 1.1010, sl=1.1030, tp=1.0950)
    bad = engine.modify_position(t, sl=1.0990, confirm=True)
    assert not bad["modified"] and "por encima del ask" in bad["error"] and not fake.sent
    r = engine.modify_position(t, sl=1.1025, confirm=True)
    assert r["modified"] and fake.sent[-1]["sl"] == 1.1025 and fake.sent[-1]["tp"] == 1.0950
    r2 = engine.modify_position(t, tp=0, confirm=True)
    assert r2["modified"] and fake.sent[-1]["tp"] == 0.0 and fake.sent[-1]["sl"] == 1.1025


def test_modify_reports_broker_rejection(engine, fake):
    t = fake.add_position("EURUSD", "buy", 0.1, 1.0990, sl=1.0970)
    fake.send_retcode = 10016
    r = engine.modify_position(t, sl=1.0985, confirm=True)
    assert r["modified"] is False and r["retcode_name"] == "INVALID_STOPS"
    assert engine.positions()[0]["sl"] == 1.0970 and engine.journal_tail()[-1]["event"] == "modify_failed"


# ---- journal, conexión, configuración ---------------------------------------------------------


def test_journal_is_jsonl(engine, tmp_path):
    engine.order_check("EURUSD", "buy", sl=1.0981)
    lines = (tmp_path / "journal" / "trades.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["event"] == "order_check"


def test_broker_reconnects_when_terminal_gone(engine, fake):
    engine.account_info()
    fake.initialized = False  # el terminal se cerró
    engine.account_info()
    assert fake.initialized


def test_broker_init_failure_message(fake, tmp_path):
    fake.fail_init = True
    eng = make_engine(fake, tmp_path)
    with pytest.raises(BrokerError, match="MT5_TERMINAL_PATH"):
        eng.account_info()


def test_broker_uses_env_credentials(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("MT5_LOGIN", "123")
    monkeypatch.setenv("MT5_PASSWORD", "x")
    monkeypatch.setenv("MT5_SERVER", "Demo")
    monkeypatch.setenv("MT5_TERMINAL_PATH", "C:\\MT5\\terminal64.exe")
    make_engine(fake, tmp_path).account_info()
    assert fake.init_kwargs == {"path": "C:\\MT5\\terminal64.exe", "login": 123, "password": "x", "server": "Demo"}


def test_load_config_from_toml(tmp_path: Path):
    p = tmp_path / "risk.toml"
    p.write_text('allow_live = false\nmax_risk_pct_per_trade = 0.5\nsymbols = ["EURUSD.m", " xauusd "]\ntrading_hours_utc = [7, 20]\n', encoding="utf-8")
    cfg = load_config(p)
    assert cfg.max_risk_pct_per_trade == 0.5 and cfg.symbols == ("EURUSD.m", "xauusd") and cfg.trading_hours_utc == (7, 20)
    assert load_config(tmp_path / "missing.toml") == RiskConfig()


def test_load_config_rejects_bad_values(tmp_path: Path):
    p = tmp_path / "risk.toml"
    p.write_text("max_risk_pct_per_trade = 50\n", encoding="utf-8")
    with pytest.raises(ValueError, match="max_risk_pct_per_trade"):
        load_config(p)


def test_repo_risk_toml_is_valid_and_demo_only():
    cfg = load_config(Path(__file__).resolve().parents[1] / "risk.toml")
    assert cfg.allow_live is False and cfg.max_risk_pct_per_trade <= 1.0
