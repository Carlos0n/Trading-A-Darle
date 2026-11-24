"""Modo automático: reglas puras y recorrido completo por el engine con el doble de MetaTrader5."""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fake_mt5 import FakeMT5

from mt5_mcp import auto as rules
from mt5_mcp.broker import BrokerError, MT5Broker
from mt5_mcp.config import AutoConfig, RiskConfig, load_config
from mt5_mcp.engine import Engine
from mt5_mcp.journal import Journal
from mt5_mcp.risk import AccountSnapshot

T0 = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc).timestamp()  # miércoles a mediodía UTC
WHY = "tendencia alcista en H1 y retroceso a la EMA20 en M15"
DEMO = AccountSnapshot(login=1, trade_mode=0, balance=10_000, equity=10_000, currency="USD")
REAL = AccountSnapshot(login=2, trade_mode=2, balance=10_000, equity=10_000, currency="USD")


class World:
    """Engine + doble de MT5 con un reloj controlado por la prueba."""

    def __init__(self, tmp_path: Path, monkeypatch, *, offset: int = 0, auto: AutoConfig | None = None, **cfg):
        self.t = T0
        self.fake = FakeMT5(server_offset=offset)
        self.fake.clock = lambda: self.t
        self.fake.touch_ticks()
        self.fake.add_symbol("GBPUSD")
        self.fake.set_tick("GBPUSD", 1.30000, 1.30010)
        base = dict(symbols=("EURUSD", "GBPUSD"), max_open_positions=3, max_volume_per_order=1.0, max_total_volume=3.0)
        base.update(cfg)
        self.cfg = RiskConfig(auto=auto or AutoConfig(enabled=True), **base)
        self.root = tmp_path
        self.engine = self.new_engine(monkeypatch)

    def new_engine(self, monkeypatch) -> Engine:
        eng = Engine(MT5Broker(module=self.fake), cfg=self.cfg, root=self.root, journal=Journal(self.root / "journal" / "trades.jsonl"))
        monkeypatch.setattr(eng, "_now", lambda: datetime.fromtimestamp(self.t, tz=timezone.utc))
        return eng

    def advance(self, minutes: float) -> None:
        self.t += minutes * 60
        self.fake.touch_ticks()

    def buy(self, symbol: str = "EURUSD", **kw):
        px = {"EURUSD": (1.0981, 1.1041), "GBPUSD": (1.2981, 1.3041)}[symbol]
        return self.engine.auto_order(symbol, "buy", kw.pop("sl", px[0]), kw.pop("tp", px[1]), kw.pop("reason", WHY))

    def events(self) -> list[str]:
        return [e["event"] for e in self.engine.journal_tail(50)]


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


# ---- reglas puras -------------------------------------------------------------------------------


def _deal(pos, entry, t, magic, profit=0.0, commission=0.0, ticket=None):
    return {"ticket": ticket or t, "type": 0, "position_id": pos, "symbol": "EURUSD", "entry": entry, "reason": 0, "volume": 0.1,
            "price": 1.1, "profit": profit, "commission": commission, "swap": 0.0, "fee": 0.0, "magic": magic, "time": t}


def test_compute_stats_attributes_by_position_not_by_exit_magic():
    day = 1_000_000
    deals = [
        _deal(1, 0, day - 500, 77), _deal(1, 1, day + 10, 0, profit=-20),            # abierta ayer por el bot, SL hoy (salida con magic 0)
        _deal(2, 0, day + 100, 77), _deal(2, 1, day + 200, 0, profit=30),            # ganadora
        _deal(3, 0, day + 300, 77), _deal(3, 1, day + 400, 77, profit=1, commission=-2),  # neto negativo por comisión
        _deal(4, 0, day + 500, 77), _deal(4, 1, day + 600, 0, profit=-5),            # perdedora
        _deal(5, 0, day + 700, 5), _deal(5, 1, day + 800, 5, profit=-99),            # manual: no cuenta
        _deal(6, 0, day + 900, 77),                                                  # del bot, sigue abierta
    ]
    positions = [{"ticket": 6, "symbol": "EURUSD", "magic": 77}, {"ticket": 9, "symbol": "GBPUSD", "magic": 5}]
    s = rules.compute_stats(deals, positions, 77, day)
    assert s.trades_today == 4 and s.last_entry_time == day + 900
    assert s.closed_today == 4 and s.wins_today == 1 and s.consecutive_losses == 2
    assert s.realized_today == pytest.approx(-20 + 30 - 1 - 5)
    assert s.open_symbols == ("EURUSD",)


def test_compute_stats_streak_resets_after_a_win_and_ignores_partially_open():
    day = 1_000_000
    deals = [
        _deal(1, 0, day + 1, 77), _deal(1, 1, day + 2, 77, profit=-10),
        _deal(2, 0, day + 3, 77), _deal(2, 1, day + 4, 77, profit=15),
        _deal(3, 0, day + 5, 77), _deal(3, 1, day + 6, 77, profit=-4),  # cierre parcial: la posición 3 sigue abierta
    ]
    s = rules.compute_stats(deals, [{"ticket": 3, "symbol": "EURUSD", "magic": 77}], 77, day)
    assert s.consecutive_losses == 0 and s.closed_today == 2 and s.trades_today == 3
    assert rules.compute_stats([], [], 77, day) == rules.AutoStats()


def test_blockers_pure():
    cfg = RiskConfig(auto=AutoConfig(enabled=True, max_trades_per_day=2, max_consecutive_losses=2, cooldown_minutes=15))
    assert rules.mode_blockers(cfg, DEMO, halted=False) == []
    assert any("desactivado" in r for r in rules.mode_blockers(RiskConfig(), DEMO, False))
    assert any("kill switch" in r for r in rules.mode_blockers(cfg, DEMO, True))
    assert any("solo opera en DEMO" in r for r in rules.mode_blockers(cfg, REAL, False))
    half = RiskConfig(allow_live=True, auto=AutoConfig(enabled=True))
    assert any("solo opera en DEMO" in r for r in rules.mode_blockers(half, REAL, False))
    both = RiskConfig(allow_live=True, auto=AutoConfig(enabled=True, allow_live=True))
    assert rules.mode_blockers(both, REAL, False) == []

    assert rules.global_entry_blockers(cfg, rules.AutoStats(), 1000.0) == []
    assert any("máximo 2" in r for r in rules.global_entry_blockers(cfg, rules.AutoStats(trades_today=2), 1000.0))
    assert any("pérdidas seguidas" in r for r in rules.global_entry_blockers(cfg, rules.AutoStats(consecutive_losses=2), 1000.0))
    st = rules.AutoStats(trades_today=1, last_entry_time=1000)
    assert rules.cooldown_remaining(cfg, st, 1000 + 5 * 60) == pytest.approx(10.0)
    assert any("faltan 10 min" in r for r in rules.global_entry_blockers(cfg, st, 1000 + 5 * 60))
    assert rules.global_entry_blockers(cfg, st, 1000 + 15 * 60) == []

    assert rules.symbol_entry_blockers(cfg, "EURUSD", ["GBPUSD"], 3.0) == []
    assert any("una por símbolo" in r for r in rules.symbol_entry_blockers(cfg, "EURUSD", ["eurusd"], 3.0))
    assert any("no se puede comprobar" in r for r in rules.symbol_entry_blockers(cfg, "EURUSD", [], None))
    assert any("máximo 60 s" in r for r in rules.symbol_entry_blockers(cfg, "EURUSD", [], 61.0))
    assert rules.rr_ratio(1.1000, 1.0980, 1.1040) == pytest.approx(2.0) and rules.rr_ratio(1.1, 1.1, 1.2) == 0.0


# ---- configuración ------------------------------------------------------------------------------


def test_auto_config_defaults_and_toml(tmp_path):
    cfg = RiskConfig()
    assert cfg.auto.enabled is False and cfg.auto.allow_live is False
    assert cfg.auto_magic == cfg.magic + 1 and cfg.auto_risk_pct == 0.5
    assert RiskConfig(max_risk_pct_per_trade=0.25).auto_risk_pct == 0.25  # nunca supera el máximo general
    p = tmp_path / "risk.toml"
    p.write_text('symbols = ["EURUSD"]\n\n[auto]\nenabled = true\nmax_trades_per_day = 3\nmin_rr = 2.0\nmagic = 555\n', encoding="utf-8")
    loaded = load_config(p)
    assert loaded.auto.enabled and loaded.auto.max_trades_per_day == 3 and loaded.auto.min_rr == 2.0 and loaded.auto_magic == 555
    p.write_text("[auto]\nenabled = true\nrisk_pct = 9\n", encoding="utf-8")
    with pytest.raises(ValueError, match="auto.risk_pct"):
        load_config(p)
    p.write_text("[auto]\nenable = true\n", encoding="utf-8")
    with pytest.raises(ValueError, match="claves desconocidas"):
        load_config(p)
    p.write_text("magic = 5\n[auto]\nmagic = 5\n", encoding="utf-8")
    with pytest.raises(ValueError, match="distinto de magic"):
        load_config(p)


def test_repo_config_keeps_auto_mode_off():
    cfg = load_config(Path(__file__).resolve().parents[1] / "risk.toml")
    assert cfg.auto.enabled is False and cfg.auto.allow_live is False


# ---- auto_order ---------------------------------------------------------------------------------


def test_auto_order_happy_path(world):
    r = world.buy()
    assert r["sent"] and r["status"] == "filled", r
    req = world.fake.sent[-1]
    assert req["magic"] == world.cfg.auto_magic and req["comment"] == "claude-auto"
    assert req["volume"] == 0.25 and req["sl"] == 1.0981 and req["tp"] == 1.1041  # 0,5 % de 10.000 con SL a 20 pips
    assert world.fake.checked[-1] == req
    last = world.engine.journal_tail(1)[0]
    assert last["event"] == "auto_order_sent" and last["reason"] == WHY and last["summary"]["risk_pct"] == pytest.approx(0.5)
    st = world.engine.auto_status()
    assert st["trades_today"] == 1 and len(st["open_auto_positions"]) == 1 and st["cooldown_remaining_minutes"] == 15.0
    assert world.engine.positions()[0]["opened_by"] == "auto"


def test_auto_order_disabled_never_reaches_broker(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, auto=AutoConfig(enabled=False))
    r = w.buy()
    assert not r["sent"] and any("modo automático desactivado" in x for x in r["reasons"])
    assert not w.fake.checked and not w.fake.sent and w.events() == ["auto_rejected"]
    st = w.engine.auto_status()
    assert st["enabled"] is False and st["can_open"] is False and st["can_manage_positions"] is False


def test_auto_order_cooldown_and_one_per_symbol(world):
    assert world.buy("EURUSD")["sent"]
    world.advance(5)
    r = world.buy("GBPUSD")
    assert not r["sent"] and any("enfriamiento" in x for x in r["reasons"]) and len(world.fake.sent) == 1
    world.advance(10)
    same = world.buy("EURUSD")
    assert not same["sent"] and any("una por símbolo" in x for x in same["reasons"])
    assert world.buy("GBPUSD")["sent"] and len(world.fake.sent) == 2


def test_auto_order_respects_manual_position_on_same_symbol(world):
    world.fake.add_position("EURUSD", "buy", 0.1, 1.0990, sl=1.0970, magic=world.cfg.magic)
    r = world.buy("EURUSD")
    assert not r["sent"] and any("una por símbolo" in x for x in r["reasons"])


def test_auto_order_daily_quota(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, auto=AutoConfig(enabled=True, max_trades_per_day=1, cooldown_minutes=0))
    assert w.buy("EURUSD")["sent"]
    w.advance(30)
    r = w.buy("GBPUSD")
    assert not r["sent"] and any("máximo 1" in x for x in r["reasons"])
    w.advance(24 * 60)  # al día siguiente el cupo vuelve
    assert w.buy("GBPUSD")["sent"]


def test_auto_order_stops_after_consecutive_losses_even_if_exit_deal_has_no_magic(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, auto=AutoConfig(enabled=True, cooldown_minutes=0, max_consecutive_losses=2, max_trades_per_day=10))
    w.fake.sltp_exit_magic = 0  # el deal de salida por SL no conserva el magic del bot
    for symbol in ("EURUSD", "GBPUSD"):
        assert w.buy(symbol)["sent"]
        w.advance(1)
        w.fake.hit_stop(w.fake._positions[-1].ticket, profit=-50.0)
        w.fake.set_account(balance=w.fake._account.balance - 50, equity=w.fake._account.equity - 50)
        w.advance(1)
    st = w.engine.auto_status()
    assert st["consecutive_losses"] == 2 and st["closed_today"] == 2 and st["realized_today_auto"] == -100.0 and not st["can_open"]
    r = w.buy("EURUSD")
    assert not r["sent"] and any("pérdidas seguidas" in x for x in r["reasons"]) and len(w.fake.sent) == 2
    w.advance(24 * 60)  # mañana se reinicia la racha
    assert w.buy("EURUSD")["sent"]


def test_auto_order_streak_resets_with_a_win(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, auto=AutoConfig(enabled=True, cooldown_minutes=0, max_consecutive_losses=2, max_trades_per_day=10))
    for profit in (-50.0, 80.0, -50.0):
        assert w.buy("EURUSD")["sent"]
        w.advance(1)
        w.fake.hit_stop(w.fake._positions[-1].ticket, profit=profit, reason=5 if profit > 0 else 4)
        w.advance(1)
    assert w.engine.auto_status()["consecutive_losses"] == 1
    assert w.buy("EURUSD")["sent"]


def test_auto_order_requires_tp_rr_reason_and_reasonable_spread(world):
    low_rr = world.buy(tp=1.1021)  # 20 pips de riesgo, 20 de beneficio: 1,0 < 1,5
    assert not low_rr["sent"] and any("beneficio/riesgo 1.00" in x for x in low_rr["reasons"])
    no_tp = world.engine.auto_order("EURUSD", "buy", 1.0981, None, WHY)
    assert not no_tp["sent"] and any("take profit obligatorio" in x for x in no_tp["reasons"])
    nan_tp = world.engine.auto_order("EURUSD", "buy", 1.0981, float("nan"), WHY)
    assert not nan_tp["sent"]
    short = world.buy(reason="porque sí")
    assert not short["sent"] and any("motivo" in x for x in short["reasons"])
    no_sl = world.engine.auto_order("EURUSD", "buy", 0, 1.1041, WHY)
    assert not no_sl["sent"] and any("stop loss obligatorio" in x for x in no_sl["reasons"])
    world.fake.add_symbol("EURUSD", spread=90)  # spread enorme frente a un SL de 200 puntos
    wide = world.buy()
    assert not wide["sent"] and any("un tercio" in x for x in wide["reasons"])
    assert not world.fake.sent and not world.fake.checked


def test_auto_order_rejects_stale_quote(world):
    world.engine.auto_status()  # fija el reloj del servidor con ticks frescos
    world.t += 10 * 60  # pasan 10 minutos sin ticks nuevos en EURUSD...
    world.fake.set_tick("GBPUSD", 1.3, 1.3001)  # ...pero GBPUSD sigue cotizando
    r = world.buy("EURUSD")
    assert not r["sent"] and any("cotización de hace 600 s" in x for x in r["reasons"]) and not world.fake.sent
    assert world.engine.quote("EURUSD")["stale"] is True and world.engine.quote("GBPUSD")["stale"] is False


def test_auto_order_when_no_symbol_is_ticking(world):
    world.engine.auto_status()
    world.t += 3 * 24 * 3600  # fin de semana largo: ningún símbolo tiene ticks recientes
    r = world.buy("EURUSD")
    assert not r["sent"] and not world.fake.sent
    assert any("no se puede comprobar" in x or "cotización de hace" in x for x in r["reasons"])


def test_auto_works_with_server_clock_ahead_of_utc(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, offset=3 * 3600)  # broker en UTC+3
    assert w.buy("EURUSD")["sent"]
    st = w.engine.auto_status()
    assert st["server_utc_offset_hours"] == 3.0 and st["server_clock_known"] and st["trades_today"] == 1
    assert st["cooldown_remaining_minutes"] == 15.0
    w.advance(16)
    assert w.buy("GBPUSD")["sent"]
    assert w.engine.quote("EURUSD")["age_seconds"] == 0 and w.engine.quote("EURUSD")["stale"] is False
    assert w.engine.auto_status()["trades_today"] == 2


def test_day_boundary_uses_utc_day_with_server_offset(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, offset=3 * 3600)
    midnight_utc = datetime(2026, 10, 21, tzinfo=timezone.utc).timestamp()
    w.fake.add_deal(profit=-70.0, time=int(midnight_utc + 3 * 3600 + 60))   # 00:01 UTC de hoy (03:01 del servidor)
    w.fake.add_deal(profit=-900.0, time=int(midnight_utc + 3 * 3600 - 60))  # 23:59 UTC de ayer (02:59 del servidor)
    w.fake.set_account(balance=9_030.0, equity=9_030.0)
    rs = w.engine.risk_status()
    assert rs["realized_today"] == -70.0 and w.engine.history_today()["count"] == 1


def test_auto_order_blocked_on_real_account_unless_both_flags(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, allow_live=True)
    w.fake.set_account(trade_mode=2)
    r = w.buy()
    assert not r["sent"] and any("solo opera en DEMO" in x for x in r["reasons"]) and not w.fake.sent
    w2 = World(tmp_path, monkeypatch, allow_live=True, auto=AutoConfig(enabled=True, allow_live=True))
    w2.fake.set_account(trade_mode=2)
    assert w2.buy()["sent"]


def test_auto_respects_general_risk_limits(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, max_open_positions=1, auto=AutoConfig(enabled=True, cooldown_minutes=0))
    assert w.buy("EURUSD")["sent"]
    w.advance(1)
    r = w.buy("GBPUSD")
    assert not r["sent"] and any("posiciones abiertas" in x for x in r["reasons"])
    lossy = World(tmp_path, monkeypatch)
    lossy.fake.add_deal(profit=-300.0)
    lossy.fake.set_account(balance=9_700.0, equity=9_700.0)
    r2 = lossy.buy()
    assert not r2["sent"] and any("pérdida diaria" in x for x in r2["reasons"]) and not lossy.fake.sent
    outside = World(tmp_path, monkeypatch, symbols=("GBPUSD",))
    r3 = outside.buy("EURUSD")
    assert not r3["sent"] and any("lista blanca" in x for x in r3["reasons"])
    off = World(tmp_path, monkeypatch)
    off.fake.set_terminal(trade_allowed=False)
    assert any("AutoTrading" in x for x in off.buy()["reasons"])


def test_auto_tools_inert_when_halted(world, monkeypatch):
    assert world.buy()["sent"]
    ticket = world.fake._positions[-1].ticket
    world.engine.halt_trading("parada del usuario")
    world.advance(20)
    sent_before = len(world.fake.sent)
    r = world.buy("GBPUSD")
    assert not r["sent"] and any("kill switch" in x for x in r["reasons"])
    assert world.engine.auto_close(ticket, "salida por regla de la estrategia")["closed"] is False
    assert world.engine.auto_tighten_stop(ticket, 1.0990, "proteger tras avanzar 1R")["modified"] is False
    assert len(world.fake.sent) == sent_before
    st = world.engine.auto_status()
    assert st["can_open"] is False and st["can_manage_positions"] is False
    assert len([b for b in st["blockers"] if "kill switch" in b]) == 1  # sin mensajes duplicados


def test_auto_order_broker_rejections_are_reported_not_retried(world):
    world.fake.check_retcode = 10018
    r = world.buy()
    assert not r["sent"] and "MARKET_CLOSED" in r["error"] and not world.fake.sent
    assert world.events()[-1] == "auto_rejected_by_broker_check"
    world.fake.check_retcode = 0
    world.fake.send_retcode = 10004
    r2 = world.buy()
    assert not r2["sent"] and r2["status"] == "failed" and r2["retcode_name"] == "REQUOTE" and len(world.fake.sent) == 1
    assert world.events()[-1] == "auto_order_failed" and "no abras otra" in r2["next"]


def test_auto_order_fails_closed_on_terminal_errors(world):
    world.fake.fail_history = True
    with pytest.raises(BrokerError):
        world.buy()
    world.fake.fail_history = False
    world.fake.fail_positions = True
    with pytest.raises(BrokerError):
        world.buy()
    assert not world.fake.sent and not world.fake.checked


# ---- auto_close y auto_tighten_stop -------------------------------------------------------------


def test_auto_close_only_touches_bot_positions(world):
    assert world.buy()["sent"]
    bot = world.fake._positions[-1].ticket
    manual = world.fake.add_position("GBPUSD", "buy", 0.1, 1.2990, sl=1.2970, magic=world.cfg.magic)
    refused = world.engine.auto_close(manual, "salida por regla de la estrategia")
    assert refused["closed"] is False and any("no la abrió el modo automático" in x for x in refused["reasons"])
    assert world.engine.auto_close(bot, "corto")["closed"] is False
    assert "no existe" in world.engine.auto_close(999, "salida por regla de la estrategia")["reasons"][0]
    sent_before = len(world.fake.sent)
    ok = world.engine.auto_close(bot, "cierre: la vela H1 cerró bajo la EMA50")
    assert ok["closed"] and len(world.fake.sent) == sent_before + 1
    req = world.fake.sent[-1]
    assert req["position"] == bot and req["type"] == world.fake.ORDER_TYPE_SELL and req["magic"] == world.cfg.auto_magic
    last = world.engine.journal_tail(1)[0]
    assert last["event"] == "auto_position_closed" and "EMA50" in last["reason"]
    assert [p["ticket"] for p in world.engine.positions()] == [manual]


def test_auto_tighten_stop_only_reduces_risk(world):
    assert world.buy()["sent"]  # compra con SL 1.0981
    t = world.fake._positions[-1].ticket
    why = "mover a breakeven tras avanzar 1R"
    widen = world.engine.auto_tighten_stop(t, 1.0950, why)
    assert widen["modified"] is False and any("solo se permite acercar" in x for x in widen["reasons"])
    same = world.engine.auto_tighten_stop(t, 1.0981, why)
    assert same["modified"] is False
    above = world.engine.auto_tighten_stop(t, 1.1050, why)
    assert above["modified"] is False and any("por debajo del bid" in x for x in above["reasons"])
    assert world.engine.auto_tighten_stop(t, float("nan"), why)["modified"] is False
    assert world.engine.auto_tighten_stop(t, 1.0990, "x")["modified"] is False
    sent_before = len(world.fake.sent)
    ok = world.engine.auto_tighten_stop(t, 1.0990, why)
    assert ok["modified"] and len(world.fake.sent) == sent_before + 1
    req = world.fake.sent[-1]
    assert req["action"] == world.fake.TRADE_ACTION_SLTP and req["sl"] == 1.099 and req["tp"] == 1.1041 and req["magic"] == world.cfg.auto_magic
    assert world.engine.journal_tail(1)[0]["event"] == "auto_stop_tightened"


def test_auto_tighten_stop_sell_side_manual_and_stops_level(world):
    r = world.engine.auto_order("EURUSD", "sell", 1.1020, 1.0960, WHY)
    assert r["sent"], r
    t = world.fake._positions[-1].ticket
    why = "mover el stop a favor tras nuevo mínimo"
    assert world.engine.auto_tighten_stop(t, 1.1030, why)["modified"] is False  # alejar el SL de una venta
    assert world.engine.auto_tighten_stop(t, 1.1010, why)["modified"] is True
    manual = world.fake.add_position("GBPUSD", "buy", 0.1, 1.2990, sl=1.2970, magic=world.cfg.magic)
    assert world.engine.auto_tighten_stop(manual, 1.2980, why)["modified"] is False
    world.fake.add_symbol("EURUSD", stops_level=50)
    close = world.engine.auto_tighten_stop(t, 1.1003, why)  # a 2 puntos del ask 1.1001: bajo el mínimo del broker
    assert close["modified"] is False and any("mínimo 50 puntos" in x for x in close["reasons"])


def test_auto_tighten_stop_reports_broker_rejection(world):
    assert world.buy()["sent"]
    t = world.fake._positions[-1].ticket
    world.fake.send_retcode = 10016
    r = world.engine.auto_tighten_stop(t, 1.0990, "mover a breakeven tras avanzar 1R")
    assert r["modified"] is False and r["retcode_name"] == "INVALID_STOPS"
    assert world.engine.journal_tail(1)[0]["event"] == "auto_modify_failed"


# ---- estado y panel -----------------------------------------------------------------------------


def test_manual_order_keeps_manual_magic_and_is_not_counted_as_auto(world):
    r = world.engine.order_send("EURUSD", "buy", sl=1.0981, confirm=True)
    assert r["sent"] and world.fake.sent[-1]["magic"] == world.cfg.magic
    st = world.engine.auto_status()
    assert st["trades_today"] == 0 and st["open_auto_positions"] == []
    assert world.engine.positions()[0]["opened_by"] == "manual"


def test_overview_is_read_only_and_complete(world):
    assert world.buy()["sent"]
    before = world.events()
    ov = world.engine.overview()
    assert set(ov) == {"generated_at_utc", "account", "risk", "auto", "positions", "journal"}
    assert ov["account"]["mode"] == "DEMO" and ov["account"]["auto_mode_enabled"] is True
    assert ov["auto"]["trades_today"] == 1 and ov["positions"][0]["opened_by"] == "auto"
    assert ov["risk"]["open_positions"] == 1 and ov["journal"][-1]["event"] == "auto_order_sent"
    assert world.events() == before and len(world.fake.sent) == 1
