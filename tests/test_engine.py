import json
from pathlib import Path

from mt5_mcp.config import RiskConfig, load_config
from mt5_mcp.engine import Engine


def test_account_info_and_risk_status(engine, fake):
    info = engine.account_info()
    assert info["mode"] == "DEMO" and info["is_demo"] and info["algo_trading_enabled"] and not info["halted"]
    rs = engine.risk_status()
    assert rs["can_open_new_positions"] and rs["daily_loss_limit"] == -300.0 and rs["pnl_today"] == 0


def test_risk_status_counts_today(engine, fake):
    fake.add_deal(profit=-120.0, commission=-2.0)
    fake.set_account(balance=9_878.0, equity=9_863.0)  # el balance ya refleja los -122 realizados
    fake.add_deal(profit=50.0, deal_type=2)  # balance deal: ignorado
    fake.add_position("EURUSD", "buy", 0.1, 1.1000, sl=1.0980, profit=-15.0)
    rs = engine.risk_status()
    assert rs["realized_today"] == -122.0 and rs["floating_pnl"] == -15.0 and rs["open_positions"] == 1
    assert rs["remaining_before_daily_halt"] == 163.0


def test_quote_and_symbol_info(engine):
    q = engine.quote("eurusd")
    assert q["bid"] == 1.1 and q["ask"] == 1.1001 and q["spread_points"] == 10
    s = engine.symbol_info("EURUSD")
    assert s["allowed_by_config"] and s["filling_chosen"] == 0


def test_position_size(engine):
    r = engine.position_size("EURUSD", "buy", sl=1.0981)
    assert r["volume"] == 0.5 and r["risk_budget"] == 100.0 and r["actual_risk"] == 100.0
    bad = engine.position_size("EURUSD", "buy", sl=1.2)
    assert "error" in bad


def test_order_check_does_not_send(engine, fake):
    r = engine.order_check("EURUSD", "buy", sl=1.0981, tp=1.1041)
    assert r["would_send"] and r["request"]["volume"] == 0.5 and r["request"]["sl"] == 1.0981
    assert fake.checked and not fake.sent


def test_order_send_requires_confirm(engine, fake):
    r = engine.order_send("EURUSD", "buy", sl=1.0981)
    assert not r["sent"] and "confirm=true" in r["error"] and not fake.sent
    assert engine.journal_tail()[-1]["event"] == "order_pending_confirmation"


def test_order_send_happy_path_builds_request(engine, fake, cfg):
    r = engine.order_send("EURUSD", "buy", sl=1.0981, tp=1.1041, comment="setup A", confirm=True)
    assert r["sent"] and r["retcode_name"] == "DONE" and r["order"] == 777
    req = fake.sent[-1]
    assert req["action"] == fake.TRADE_ACTION_DEAL and req["type"] == fake.ORDER_TYPE_BUY
    assert req["price"] == 1.1001 and req["volume"] == 0.5 and req["sl"] == 1.0981 and req["tp"] == 1.1041
    assert req["magic"] == cfg.magic and req["deviation"] == cfg.max_slippage_points and req["type_filling"] == fake.ORDER_FILLING_FOK
    assert fake.checked[-1] == req
    assert engine.positions()[0]["ticket"] == 1000
    events = [e["event"] for e in engine.journal_tail()]
    assert events[-1] == "order_sent"


def test_order_send_sell_uses_bid(engine, fake):
    r = engine.order_send("EURUSD", "sell", sl=1.1020, confirm=True)
    assert r["sent"] and fake.sent[-1]["price"] == 1.1 and fake.sent[-1]["type"] == fake.ORDER_TYPE_SELL


def test_order_send_rejected_by_risk_never_reaches_broker(engine, fake):
    fake.add_symbol("USDJPY", digits=3, point=0.001, tick_size=0.001, tick_value=0.65)
    fake.set_tick("USDJPY", 150.000, 150.010)
    r = engine.order_send("USDJPY", "buy", sl=149.500, confirm=True)  # USDJPY no está en la lista blanca del test
    assert not r["sent"] and not fake.sent and not fake.checked
    assert engine.journal_tail()[-1]["event"] == "order_rejected_by_risk"


def test_order_send_live_account_blocked(fake, cfg, tmp_path):
    fake.set_account(trade_mode=2)
    from mt5_mcp.broker import MT5Broker

    eng = Engine(MT5Broker(module=fake), cfg=cfg, root=tmp_path)
    r = eng.order_send("EURUSD", "buy", sl=1.0981, confirm=True)
    assert not r["sent"] and any("REAL" in x for x in r["evaluation"]["reasons"]) and not fake.sent


def test_order_send_broker_check_failure(engine, fake):
    fake.check_retcode = 10019
    r = engine.order_send("EURUSD", "buy", sl=1.0981, confirm=True)
    assert not r["sent"] and "NO_MONEY" in r["error"] and not fake.sent


def test_order_send_broker_failure_reported(engine, fake):
    fake.send_retcode = 10004
    r = engine.order_send("EURUSD", "buy", sl=1.0981, confirm=True)
    assert not r["sent"] and r["retcode_name"] == "REQUOTE" and "no reintentes" in r["next"]


def test_halt_blocks_new_orders_but_allows_close(engine, fake, tmp_path):
    t = fake.add_position("EURUSD", "buy", 0.1, 1.0990, sl=1.0970)
    res = engine.halt_trading(reason="prueba")
    assert res["halted"] and (tmp_path / "HALT").exists()
    r = engine.order_send("EURUSD", "buy", sl=1.0981, confirm=True)
    assert not r["sent"] and any("kill switch" in x for x in r["evaluation"]["reasons"])
    c = engine.close_position(t, confirm=True)
    assert c["closed"] and fake.sent[-1]["position"] == t and fake.sent[-1]["type"] == fake.ORDER_TYPE_SELL and fake.sent[-1]["price"] == 1.1
    assert engine.positions() == []


def test_halt_close_all(engine, fake):
    fake.add_position("EURUSD", "buy", 0.1, 1.0990, sl=1.0970)
    fake.add_position("EURUSD", "sell", 0.2, 1.1010, sl=1.1030)
    res = engine.halt_trading(reason="cerrar todo", close_all=True)
    assert len(res["closed"]) == 2 and all(c["closed"] for c in res["closed"]) and engine.positions() == []


def test_close_requires_confirm_and_valid_ticket(engine, fake):
    t = fake.add_position("EURUSD", "buy", 0.1, 1.0990, sl=1.0970)
    assert not engine.close_position(t)["closed"] and not fake.sent
    assert "no existe" in engine.close_position(99)["error"]


def test_modify_position_rules(engine, fake):
    t = fake.add_position("EURUSD", "buy", 0.1, 1.0990, sl=1.0970)
    assert "quitar el stop" in engine.modify_position(t, sl=0, confirm=True)["error"]
    assert "por debajo" in engine.modify_position(t, sl=1.1050, confirm=True)["error"]
    assert not engine.modify_position(t, sl=1.0985)["modified"]
    r = engine.modify_position(t, sl=1.0985, tp=1.1100, confirm=True)
    assert r["modified"] and fake.sent[-1]["action"] == fake.TRADE_ACTION_SLTP and fake.sent[-1]["sl"] == 1.0985
    assert engine.positions()[0]["sl"] == 1.0985


def test_journal_is_jsonl(engine, tmp_path):
    engine.order_check("EURUSD", "buy", sl=1.0981)
    lines = (tmp_path / "journal" / "trades.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["event"] == "order_check"


def test_broker_reconnects_when_terminal_gone(engine, fake):
    engine.account_info()
    fake.initialized = False  # el terminal se cerró
    engine.account_info()
    assert fake.initialized


def test_broker_init_failure_message(fake, cfg, tmp_path):
    from mt5_mcp.broker import BrokerError, MT5Broker

    fake.fail_init = True
    eng = Engine(MT5Broker(module=fake), cfg=cfg, root=tmp_path)
    try:
        eng.account_info()
    except BrokerError as exc:
        assert "-10005" in str(exc) and "MT5_TERMINAL_PATH" in str(exc)
    else:
        raise AssertionError("debió fallar")


def test_broker_uses_env_credentials(fake, cfg, tmp_path, monkeypatch):
    from mt5_mcp.broker import MT5Broker

    monkeypatch.setenv("MT5_LOGIN", "123")
    monkeypatch.setenv("MT5_PASSWORD", "x")
    monkeypatch.setenv("MT5_SERVER", "Demo")
    monkeypatch.setenv("MT5_TERMINAL_PATH", "C:\\\\MT5\\\\terminal64.exe")
    Engine(MT5Broker(module=fake), cfg=cfg, root=tmp_path).account_info()
    assert fake.init_kwargs == {"path": "C:\\\\MT5\\\\terminal64.exe", "login": 123, "password": "x", "server": "Demo"}


def test_load_config_from_toml(tmp_path: Path):
    p = tmp_path / "risk.toml"
    p.write_text('allow_live = false\nmax_risk_pct_per_trade = 0.5\nsymbols = ["eurusd"]\ntrading_hours_utc = [7, 20]\n', encoding="utf-8")
    cfg = load_config(p)
    assert cfg.max_risk_pct_per_trade == 0.5 and cfg.symbols == ("EURUSD",) and cfg.trading_hours_utc == (7, 20)
    assert load_config(tmp_path / "missing.toml") == RiskConfig()


def test_load_config_rejects_bad_values(tmp_path: Path):
    p = tmp_path / "risk.toml"
    p.write_text("max_risk_pct_per_trade = 50\n", encoding="utf-8")
    try:
        load_config(p)
    except ValueError as exc:
        assert "max_risk_pct_per_trade" in str(exc)
    else:
        raise AssertionError("debió fallar")


def test_repo_risk_toml_is_valid_and_demo_only():
    cfg = load_config(Path(__file__).resolve().parents[1] / "risk.toml")
    assert cfg.allow_live is False and cfg.max_risk_pct_per_trade <= 1.0
