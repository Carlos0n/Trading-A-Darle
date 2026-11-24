"""Modo automático: reglas puras y recorrido completo por el engine con el doble de MetaTrader5."""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import WHY, FailingJournal, World
from test_engine import DISTANCES, INDICES, add_index, assert_sent_as_sized, off_grid_levels, stop_during_broker_check

from mt5_mcp import auto as rules
from mt5_mcp.broker import BrokerError
from mt5_mcp.config import AutoConfig, RiskConfig, load_config
from mt5_mcp.engine import LOOKBACK_DAYS, SYMBOLS_EMPTY_MSG
from mt5_mcp.risk import AccountSnapshot

DEMO = AccountSnapshot(login=1, trade_mode=0, balance=10_000, equity=10_000, currency="USD")
REAL = AccountSnapshot(login=2, trade_mode=2, balance=10_000, equity=10_000, currency="USD")


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
    # los minutos que faltan se dicen hacia arriba y nunca como "0 min": mientras quede espera no se puede abrir
    waits = {elapsed: rules.global_entry_blockers(cfg, st, 1000 + elapsed)[0] for elapsed in (0, 20, 5 * 60 + 36, 14 * 60, 14 * 60 + 40, 899)}
    assert [w.split(" desde")[0] for w in waits.values()] == [
        "enfriamiento: faltan 15 min", "enfriamiento: faltan 15 min", "enfriamiento: faltan 10 min", "enfriamiento: faltan 1 min",
        "enfriamiento: faltan menos de 1 min", "enfriamiento: faltan menos de 1 min"]
    assert all(w.endswith("desde la última entrada (mínimo 15 min)") for w in waits.values())
    # reloj del PC 20 s por detrás del servidor: la entrada parece posterior a "ahora"; la espera no pasa del mínimo
    assert rules.cooldown_remaining(cfg, st, 1000 - 20) == 15.0
    assert rules.global_entry_blockers(cfg, st, 1000 - 20)[0].startswith("enfriamiento: faltan 15 min desde")

    assert rules.symbol_entry_blockers(cfg, "EURUSD", ["GBPUSD"], 3.0) == []
    assert any("una por símbolo" in r for r in rules.symbol_entry_blockers(cfg, "EURUSD", ["eurusd"], 3.0))
    unknown_age = rules.symbol_entry_blockers(cfg, "EURUSD", [], None)
    assert len(unknown_age) == 1 and "no se puede comprobar" in unknown_age[0]
    assert "hora del servidor desconocida" in unknown_age[0] and "mercado" not in unknown_age[0]  # no afirma que el mercado esté cerrado
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


def test_cooldown_minutes_and_its_blocker_text_agree_and_never_say_zero(world):
    # Con cooldown_minutes = 15 y un ciclo cada 15 min, el ciclo siguiente a una entrada llega con unos segundos de espera:
    # el estado no puede decir "faltan 0 min" ni dar 0 minutos mientras el bloqueo siga puesto.
    assert world.buy("EURUSD")["sent"]
    entry = world.t  # instante de la entrada (tras la espera del arranque a que se moviera un tick)
    seen = {}
    for seconds in (5 * 60 + 58, 14 * 60 + 46, 14 * 60 + 58):  # quedan 9 min 2 s, 14 s y 2 s
        world.t = entry + seconds
        st = world.engine.auto_status()
        seen[seconds] = (st["cooldown_remaining_minutes"], [b.split(" desde")[0] for b in st["blockers"]], st["can_open"])
    assert seen == {
        5 * 60 + 58: (9.1, ["enfriamiento: faltan 10 min"], False),
        14 * 60 + 46: (0.3, ["enfriamiento: faltan menos de 1 min"], False),
        14 * 60 + 58: (0.1, ["enfriamiento: faltan menos de 1 min"], False),
    }
    world.t = entry + 15 * 60
    st = world.engine.auto_status()
    assert st["cooldown_remaining_minutes"] == 0.0 and st["blockers"] == [] and st["can_open"] is True


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
    world.engine.auto_status()  # fija el reloj del servidor con el mercado abierto
    world.fake.freeze_ticks("EURUSD")  # EURUSD deja de cotizar...
    world.advance(10)  # ...y pasan 10 minutos; GBPUSD sigue cotizando
    r = world.buy("EURUSD")
    assert not r["sent"] and any("cotización de hace 600 s" in x for x in r["reasons"]) and not world.fake.sent
    assert world.engine.quote("EURUSD")["stale"] is True and world.engine.quote("GBPUSD")["stale"] is False


def test_auto_order_when_no_symbol_is_ticking(world):
    world.engine.auto_status()
    world.fake.freeze_ticks()  # el mercado cierra...
    world.advance(3 * 24 * 60)  # ...y pasa un fin de semana largo
    r = world.buy("EURUSD")
    assert not r["sent"] and not world.fake.sent and not world.fake.checked
    assert any("hora del servidor desconocida" in x for x in r["reasons"])
    assert any("no se puede comprobar" in x for x in r["reasons"])


def test_status_of_a_long_lived_process_does_not_promise_entries_when_no_listed_symbol_is_quoting(world):
    # Panel o sesión de Claude Code abiertos cuando cierra el mercado: la hora del servidor sigue conocida durante
    # horas, pero auto_order rechazaría cualquier símbolo por cotización vieja. can_open debe decir lo mismo.
    assert world.engine.auto_status()["can_open"] is True
    world.fake.freeze_ticks()  # el mercado cierra
    world.advance(1)  # 60 s sin cotizaciones: justo en el máximo
    st = world.engine.auto_status()
    assert st["server_clock_known"] and st["can_open"] is True and st["blockers"] == []
    seen = []
    for minutes in (0.1, 5, 4 * 60):
        world.advance(minutes)
        st = world.engine.auto_status()
        assert st["server_clock_known"] and st["can_open"] is False and st["can_manage_positions"] is True and len(st["blockers"]) == 1
        seen.append(st["blockers"][0])
    assert seen == [f"sin cotizaciones recientes en los símbolos de `symbols`: la última llegó hace {ago} (máximo 60 s); mercado parado o sin ticks"
                    for ago in ("66 s", "6 min", "4 h")]
    for symbol in ("EURUSD", "GBPUSD"):  # y es verdad: el servidor rechaza la entrada en cualquiera de los dos
        r = world.buy(symbol)
        assert not r["sent"] and any("cotización de hace" in x and "mercado parado o sin ticks" in x for x in r["reasons"])
    assert not world.fake.sent and not world.fake.checked
    assert "sin cotizaciones recientes" not in " ".join(world.engine.risk_status()["blockers"])  # las órdenes manuales no dependen de esto
    world.fake.touch_ticks("GBPUSD")  # basta con que cotice uno de la lista
    st = world.engine.auto_status()
    assert st["can_open"] is True and st["blockers"] == []
    assert world.buy("GBPUSD")["sent"]


@pytest.mark.parametrize("skew", [61, 90, 119])
def test_pc_clock_fast_within_the_tolerance_is_not_called_a_stopped_market(tmp_path, monkeypatch, skew):
    # El reloj de Windows va `skew` s adelantado: la hora del servidor se acepta (tolera hasta 120 s), pero cada
    # cotización, recién llegada, parece tener `skew` s. El mercado cotiza: decir "mercado parado" sería falso.
    w = World(tmp_path, monkeypatch, offset=3 * 3600)
    monkeypatch.setattr(w.engine, "_now", lambda: datetime.fromtimestamp(w.t + skew, tz=timezone.utc))
    st = w.engine.auto_status()
    assert st["server_clock_known"] is True and st["can_open"] is False and len(st["blockers"]) == 1
    status = st["blockers"][0]
    assert status.startswith(f"llegan cotizaciones de EURUSD, GBPUSD, pero este PC las ve con {skew} s de retraso (máximo 60 s): ")
    assert "su reloj va adelantado" in status and "el terminal acaba de reconectar" in status
    assert status.endswith("sincroniza la hora de Windows") and "mercado parado o sin ticks" not in status
    r = w.buy("EURUSD")
    assert r["sent"] is False and not w.fake.sent and not w.fake.checked and len(r["reasons"]) == 1
    reason = r["reasons"][0]
    assert reason.startswith(f"cotización de hace {skew} s (máximo 60 s) aunque acaba de llegar: el reloj de este PC va adelantado")
    assert "mercado parado o sin ticks" not in reason and "sincroniza la hora de Windows" in reason
    assert w.engine.order_send("EURUSD", "buy", sl=1.0981, confirm=True)["sent"]  # una orden confirmada a mano no mira la antigüedad


def test_a_stopped_market_is_still_called_stopped_even_if_old_quotes_arrive_on_reconnecting(world):
    assert world.engine.auto_status()["can_open"] is True  # proceso de larga vida: conoce la hora del servidor
    world.fake.freeze_ticks()  # el mercado cierra
    world.advance(5)
    stopped = "sin cotizaciones recientes en los símbolos de `symbols`: la última llegó hace {} (máximo 60 s); mercado parado o sin ticks"
    st = world.engine.auto_status()
    assert st["server_clock_known"] and st["blockers"] == [stopped.format("5 min")]
    r = world.buy("EURUSD")
    assert r["reasons"] == ["cotización de hace 300 s (máximo 60 s): mercado parado o sin ticks"]
    # El terminal reconecta y recibe las últimas cotizaciones del servidor, de hace 4 minutos: cambian entre dos lecturas
    # cercanas, pero no acaban de llegar al mercado. No es el reloj del PC: el mercado sigue parado.
    for name, (bid, ask) in {"EURUSD": (1.1, 1.1001), "GBPUSD": (1.3, 1.3001)}.items():
        world.fake.set_tick(name, bid, ask, time=world.fake.server_now() - 240)
    world.t += 3
    st = world.engine.auto_status()
    assert st["server_clock_known"] and st["blockers"] == [stopped.format("4 min")]
    assert not any("llegan cotizaciones de" in b for b in st["blockers"])
    r = world.buy("EURUSD")
    assert r["sent"] is False and r["reasons"] == ["cotización de hace 243 s (máximo 60 s): mercado parado o sin ticks"]
    assert not world.fake.sent and not world.fake.checked


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


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("name", sorted(INDICES))
def test_auto_order_sizes_with_the_stop_on_the_tick_grid(tmp_path, monkeypatch, name, side):
    w = World(tmp_path, monkeypatch, symbols=(name, "EURUSD"), max_open_positions=5, max_volume_per_order=50.0, max_total_volume=100.0,
              auto=AutoConfig(enabled=True, cooldown_minutes=0, max_trades_per_day=50, one_position_per_symbol=False))
    tick_size, tick_value = add_index(w.fake, name)
    for distance in DISTANCES[name]:
        sl, tp = off_grid_levels(name, side, distance)
        r = w.engine.auto_order(name, side, sl, tp, WHY)
        assert r["sent"], (distance, r)
        assert_sent_as_sized(w.fake.sent[-1], r["summary"], 50.0, tick_size, tick_value)  # [auto] risk_pct: 0,5 % de 10.000
        assert w.engine.journal_tail(1)[0]["summary"] == r["summary"]
        w.fake._positions.clear()


def test_auto_order_is_not_sent_if_halt_appears_while_it_is_being_prepared(world, monkeypatch):
    assert world.buy("GBPUSD")["sent"]
    bot = world.fake._positions[-1].ticket
    world.advance(16)
    stop_during_broker_check(world.fake, world.root / "HALT", monkeypatch)
    before = world.events()
    r = world.buy("EURUSD")
    assert r["sent"] is False and r["status"] == "rejected" and "kill switch se activó mientras se preparaba" in r["error"]
    assert "termina el ciclo" in r["next"] and "position_found" not in r
    assert len(world.fake.checked) == 2 and len(world.fake.sent) == 1 and [p.ticket for p in world.fake._positions] == [bot]
    assert world.events()[len(before):] == ["auto_rejected_by_halt"] and world.engine._auto_ids() == frozenset({bot})
    st = world.engine.auto_status()
    assert st["can_open"] is False and st["can_manage_positions"] is False
    # el usuario sí puede reducir riesgo: mover el stop a mano y parar cerrándolo todo
    assert world.engine.modify_position(bot, sl=1.2985, confirm=True)["modified"] is True
    res = world.engine.halt_trading("parada del usuario", close_all=True, confirm=True)
    assert res["halted"] and [c["ticket"] for c in res["closed"]] == [bot] and res["closed"][0]["closed"]
    assert world.engine.positions() == []


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


# ---- identidad de las posiciones del bot ----------------------------------------------------------


def test_compute_stats_uses_identifier_and_known_ids():
    day = 1_000_000
    deals = [
        _deal(500, 0, day + 1, 77), _deal(500, 1, day + 2, 0, profit=-10),   # ticket 9 != identificador 500, ya cerrada
        _deal(600, 0, day + 3, 0), _deal(600, 1, day + 4, 0, profit=-10),    # el broker borró el magic: solo el registro la reconoce
        _deal(700, 1, day + 5, 0, profit=-10),                               # abierta antes de la ventana: sin deal de entrada
        _deal(800, 0, day + 6, 77),                                          # sigue abierta, con ticket distinto
    ]
    positions = [{"ticket": 42, "identifier": 800, "symbol": "EURUSD", "magic": 77}]
    plain = rules.compute_stats(deals, positions, 77, day)
    assert plain.trades_today == 2 and plain.closed_today == 1 and plain.consecutive_losses == 1 and len(plain.open_positions) == 1
    known = rules.compute_stats(deals, positions, 77, day, frozenset({600, 700}))
    assert known.trades_today == 3 and known.closed_today == 3 and known.consecutive_losses == 3
    assert rules.is_bot_position({"ticket": 1, "identifier": 600, "magic": 0}, 77, frozenset({600}))
    assert not rules.is_bot_position({"ticket": 600, "identifier": 601, "magic": 0}, 77, frozenset({600}))
    assert rules.is_bot_position({"ticket": 1, "magic": 77}, 77)


def test_bot_keeps_counting_when_broker_wipes_the_magic(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, auto=AutoConfig(enabled=True, cooldown_minutes=15, max_consecutive_losses=2, max_trades_per_day=10))
    w.fake.wipe_magic = True
    assert w.buy("EURUSD")["sent"]
    pos = w.fake._positions[-1]
    assert pos.magic == 0  # el broker no conservó el identificador del bot
    st = w.engine.auto_status()
    assert st["trades_today"] == 1 and len(st["open_auto_positions"]) == 1 and st["cooldown_remaining_minutes"] == 15.0
    assert w.engine.positions()[0]["opened_by"] == "auto"
    w.advance(5)
    assert any("enfriamiento" in x for x in w.buy("GBPUSD")["reasons"])
    assert w.engine.auto_tighten_stop(pos.ticket, 1.0990, "mover a breakeven tras avanzar 1R")["modified"] is True
    w.fake.hit_stop(pos.ticket, profit=-50.0)
    w.advance(20)
    assert w.buy("GBPUSD")["sent"]
    w.advance(1)
    w.fake.hit_stop(w.fake._positions[-1].ticket, profit=-50.0)
    w.advance(20)
    st = w.engine.auto_status()
    assert st["consecutive_losses"] == 2 and st["closed_today"] == 2 and st["trades_today"] == 2
    assert any("pérdidas seguidas" in x for x in w.buy("EURUSD")["reasons"])


def test_registry_survives_a_server_restart_and_ignores_garbage(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.fake.wipe_magic = True
    assert w.buy("EURUSD")["sent"]
    registry = tmp_path / "journal" / "auto_positions.jsonl"
    lines = registry.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and '"id": 1000' in lines[0]
    with registry.open("a", encoding="utf-8") as fh:
        fh.write("esto no es json\n{\"id\": \"x\"}\n{\"id\": 5, \"ts\": 1}\n")  # basura y un registro caducado
    fresh = w.new_engine(monkeypatch)  # como si el servidor MCP arrancara de nuevo en el siguiente ciclo
    st = fresh.auto_status()
    assert st["trades_today"] == 1 and len(st["open_auto_positions"]) == 1
    assert fresh._auto_ids() == frozenset({1000})
    ok = fresh.auto_close(1000, "cierre: la tendencia H1 se dio la vuelta")
    assert ok["closed"] is True


def test_failed_or_rejected_auto_orders_are_not_registered(world):
    world.fake.send_retcode = 10004
    assert world.buy()["sent"] is False
    world.fake.send_retcode = 10009
    assert world.buy(reason="x")["sent"] is False
    assert not (world.root / "journal" / "auto_positions.jsonl").exists()


def test_manual_position_is_never_adopted_by_the_bot(world):
    manual = world.fake.add_position("GBPUSD", "buy", 0.1, 1.2990, sl=1.2970, magic=0)
    assert world.buy("EURUSD")["sent"]
    assert world.engine.auto_close(manual, "salida por regla de la estrategia")["closed"] is False
    owners = {p["ticket"]: p["opened_by"] for p in world.engine.positions()}
    assert owners[manual] == "externo" and "auto" in owners.values()


def test_position_opened_weeks_ago_and_closed_today_counts_in_the_streak(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, auto=AutoConfig(enabled=True, cooldown_minutes=0, max_consecutive_losses=1))
    old = w.fake.server_now() - 20 * 86400
    w.fake.add_deal(profit=0.0, entry=0, position_id=4242, magic=w.cfg.auto_magic, time=old)
    w.fake.add_deal(profit=-80.0, entry=1, position_id=4242, magic=0, reason=4)
    w.fake.set_account(balance=9_920.0, equity=9_920.0)
    st = w.engine.auto_status()
    assert st["closed_today"] == 1 and st["consecutive_losses"] == 1 and st["trades_today"] == 0
    assert any("pérdidas seguidas" in x for x in w.buy()["reasons"])


def test_position_opened_before_the_history_window_and_closed_today_counts_in_the_streak(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, auto=AutoConfig(enabled=True, cooldown_minutes=0, max_consecutive_losses=1))
    old = w.fake.server_now() - (LOOKBACK_DAYS + 30) * 86400  # la entrada queda fuera de la ventana que se lee
    w.fake.add_deal(profit=0.0, commission=-1.5, entry=0, position_id=4242, magic=w.cfg.auto_magic, time=old)
    w.fake.add_deal(profit=-80.0, entry=1, position_id=4242, magic=0, reason=4)  # SL hoy; la salida no conserva el magic
    w.fake.add_deal(profit=0.0, entry=0, position_id=5151, magic=w.cfg.magic, time=old)  # otra igual de vieja, pero manual
    w.fake.add_deal(profit=-30.0, entry=1, position_id=5151, magic=0, reason=4)
    w.fake.add_deal(profit=-9.0, entry=1, position_id=0)  # deal de salida sin posición: no se consulta nada por él
    w.fake.set_account(balance=9_880.5, equity=9_880.5)
    asked = []
    orig = w.fake.history_deals_get
    monkeypatch.setattr(w.fake, "history_deals_get", lambda *a, **k: asked.append(k.get("position")) or orig(*a, **k))
    st = w.engine.auto_status()
    assert st["closed_today"] == 1 and st["consecutive_losses"] == 1 and st["trades_today"] == 0 and st["wins_today"] == 0
    assert st["realized_today_auto"] == -81.5  # el neto incluye la comisión de la entrada antigua, sin contar dos veces la salida
    assert not st["can_open"] and any("pérdidas seguidas" in b for b in st["blockers"])
    assert w.engine.risk_status()["realized_today"] == -119.0  # el resultado del día no cambia: solo los deals de hoy
    assert any("pérdidas seguidas" in x for x in w.buy()["reasons"]) and not w.fake.sent
    assert sorted(x for x in asked if x is not None) == [4242, 5151]  # una consulta por posición, no una por llamada


def test_history_error_for_an_old_position_fails_closed(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.fake.add_deal(profit=-80.0, entry=1, position_id=4242, magic=0, reason=4)
    orig = w.fake.history_deals_get
    monkeypatch.setattr(w.fake, "history_deals_get", lambda *a, **k: None if k.get("position") else orig(*a, **k))
    with pytest.raises(BrokerError, match="position=4242"):
        w.engine.auto_status()
    with pytest.raises(BrokerError):
        w.buy()
    assert not w.fake.sent and not w.fake.checked


# ---- lista de símbolos y volumen total ----------------------------------------------------------------


def test_auto_mode_needs_a_symbols_list(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, symbols=())
    bot = w.fake.add_position("GBPUSD", "buy", 0.1, 1.2990, sl=1.2970, tp=1.3100, magic=w.cfg.auto_magic)
    st = w.engine.auto_status()
    assert st["server_clock_known"] is True  # con la lista vacía el reloj se apoya en Observación de Mercado
    assert st["can_open"] is False and SYMBOLS_EMPTY_MSG in st["blockers"] and len(st["blockers"]) == 1
    assert st["can_manage_positions"] is True and len(st["open_auto_positions"]) == 1  # es bloqueo de entrada, no de modo
    r = w.buy("EURUSD")
    assert not r["sent"] and SYMBOLS_EMPTY_MSG in r["reasons"] and not w.fake.sent and not w.fake.checked
    assert w.engine.auto_tighten_stop(bot, 1.2980, "mover a breakeven tras avanzar 1R")["modified"] is True
    assert w.engine.auto_close(bot, "cierre: la tendencia H1 se dio la vuelta")["closed"] is True
    off = World(tmp_path, monkeypatch, symbols=(), auto=AutoConfig(enabled=False))
    assert SYMBOLS_EMPTY_MSG not in off.engine.auto_status()["blockers"]  # con el modo apagado solo se dice que está apagado


def test_auto_status_explains_symbols_that_do_not_exist_at_this_broker(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, symbols=("EURUSD.pro", "XAUUSD.pro"))  # la lista no lleva los nombres de este broker
    st = w.engine.auto_status()
    assert st["can_open"] is False and st["can_manage_positions"] is True and st["server_clock_known"] is False
    assert any("ningún símbolo de `symbols` existe" in b for b in st["blockers"])
    assert any("no hay ningún símbolo de referencia" in b for b in st["blockers"])
    assert w.slept == []  # sin referencias no hay nada que esperar
    one = World(tmp_path, monkeypatch, symbols=("EURUSD.pro", "GBPUSD"))  # con que uno exista, el aviso no sale
    st2 = one.engine.auto_status()
    assert st2["can_open"] is True and st2["blockers"] == []


def test_auto_status_cannot_open_when_total_volume_is_at_the_maximum(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, max_total_volume=0.25, auto=AutoConfig(enabled=True, cooldown_minutes=0))
    assert w.engine.auto_status()["can_open"] is True
    assert w.buy("EURUSD")["sent"]  # 0,25 lotes: el volumen total queda justo en el máximo
    st = w.engine.auto_status()
    assert st["can_open"] is False and st["blockers"] == ["volumen total abierto 0.25 ya alcanza max_total_volume=0.25"]
    r = w.buy("GBPUSD")
    assert not r["sent"] and any("supera max_total_volume=0.25" in x for x in r["reasons"])


# ---- hora del servidor desconocida ----------------------------------------------------------------


def test_auto_status_with_unknown_clock_shows_the_real_reason_not_a_made_up_cooldown(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, offset=3 * 3600, auto=AutoConfig(enabled=True, max_trades_per_day=1))
    assert w.buy("EURUSD")["sent"]
    w.fake.freeze_ticks()
    w.advance(13 * 60)  # 13 h sin ticks: el desfase guardado caduca
    st = w.new_engine().auto_status()
    assert st["server_clock_known"] is False and st["can_open"] is False and st["cooldown_remaining_minutes"] is None
    assert len(st["blockers"]) == 1 and st["blockers"][0].startswith("hora del servidor desconocida")
    assert st["can_manage_positions"] is True and len(st["open_auto_positions"]) == 1  # sus posiciones se siguen gestionando
    ticket = st["open_auto_positions"][0]["ticket"]
    assert w.engine.auto_tighten_stop(ticket, 1.0990, "mover a breakeven tras avanzar 1R")["modified"] is True
    assert w.engine.auto_close(ticket, "cierre: la tendencia H1 se dio la vuelta")["closed"] is True


# ---- el journal no decide lo que pasa con una orden -------------------------------------------------


def test_auto_order_is_not_sent_if_the_attempt_cannot_be_journaled(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.engine = w.new_engine(journal=FailingJournal(tmp_path / "journal" / "trades.jsonl"))
    r = w.buy()
    assert r["sent"] is False and r["status"] == "rejected" and "NO enviada" in r["error"] and "No space left" in r["journal_error"]
    assert w.fake.checked and not w.fake.sent and w.engine.positions() == []
    assert not (tmp_path / "journal" / "auto_positions.jsonl").exists()
    assert "termina el ciclo" in r["next"]


def test_auto_order_result_survives_a_journal_failure_after_sending(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.engine = w.new_engine(journal=FailingJournal(tmp_path / "journal" / "trades.jsonl", ok_writes=1))
    r = w.buy()
    assert r["sent"] is True and r["status"] == "filled" and r["order"] == 1000 and r["summary"]["volume"] == 0.25
    assert "No space left" in r["journal_error"] and len(w.fake.sent) == 1
    assert w.events() == ["auto_order_attempt"]
    st = w.engine.auto_status()  # el cupo sale del terminal, no del journal
    assert st["trades_today"] == 1 and len(st["open_auto_positions"]) == 1
    assert w.engine._auto_ids() == frozenset({1000})


def test_auto_order_reports_a_registry_failure_without_hiding_the_order(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    (tmp_path / "journal" / "auto_positions.jsonl").mkdir(parents=True)  # el registro no se puede escribir
    r = w.buy()
    assert r["sent"] is True and r["order"] == 1000 and "registro de posiciones del bot" in r["journal_error"]
    assert w.events() == ["auto_order_attempt", "auto_order_sent"]
    assert len(w.engine.auto_status()["open_auto_positions"]) == 1  # sigue reconocida por su magic


def test_auto_order_without_answer_is_unknown_and_must_not_be_repeated(world):
    world.fake.send_none = True
    r = world.buy()
    assert r["sent"] is False and r["status"] == "unknown" and r["retcode_name"] == "SIN_RESPUESTA"
    assert "PUDO llegar al broker" in r["error"] and "positions()" in r["next"] and "no abras otra" in r["next"]
    assert world.events() == ["auto_order_attempt", "auto_order_unknown"]
    assert world.engine.journal_tail(1)[0]["reason"] == WHY
    world.fake.send_none = False
    st = world.engine.auto_status()  # la orden sí se ejecutó: el siguiente estado la ve y el cupo la cuenta
    assert st["trades_today"] == 1 and len(st["open_auto_positions"]) == 1
    again = world.buy()
    assert not again["sent"] and len(world.fake.sent) == 1


def test_unanswered_auto_order_at_a_broker_that_wipes_the_magic_still_counts_for_the_bot(tmp_path, monkeypatch):
    # Dos fallos a la vez: el broker borra el magic y order_send no contesta tras abrir. Sin el registro del bot nada
    # une esa posición con él: el ciclo siguiente no la contaría en el cupo ni en el enfriamiento y abriría otra.
    w = World(tmp_path, monkeypatch, auto=AutoConfig(enabled=True, max_trades_per_day=1, cooldown_minutes=60))
    w.fake.wipe_magic = True
    w.fake.send_none = True
    r = w.buy("EURUSD")
    assert r["sent"] is False and r["status"] == "unknown" and r["position_found"] == [1000] and "journal_error" not in r
    assert "hay una posición nueva en EURUSD (ticket 1000), anotada como del bot" in r["next"] and "no abras otra" in r["next"]
    assert w.fake._positions[-1].magic == 0 and w.engine._auto_ids() == frozenset({1000})
    assert w.events() == ["auto_order_attempt", "auto_order_unknown"]
    w.fake.send_none = False
    w.advance(15)
    w.engine = w.new_engine()  # el ciclo siguiente es otro proceso
    st = w.engine.auto_status()
    assert st["trades_today"] == 1 and st["cooldown_remaining_minutes"] == 45.0 and st["can_open"] is False
    assert [p["ticket"] for p in st["open_auto_positions"]] == [1000]
    again = w.buy("GBPUSD")
    assert again["sent"] is False and any("operaciones automáticas hoy (máximo 1)" in x for x in again["reasons"])
    assert len(w.fake.sent) == 1 and w.engine.positions()[0]["opened_by"] == "auto"
    assert w.engine.auto_close(1000, "cierre: la tendencia H1 se dio la vuelta")["closed"] is True  # y el bot la puede gestionar


def test_unanswered_auto_order_does_not_adopt_a_position_that_was_already_open(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, auto=AutoConfig(enabled=True, one_position_per_symbol=False))
    w.fake.wipe_magic = True
    manual = w.fake.add_position("EURUSD", "buy", 0.1, 1.0990, sl=1.0970, magic=0)  # mismo símbolo y lado, abierta a mano
    w.fake.send_none = True
    r = w.buy("EURUSD")
    assert r["status"] == "unknown" and r["position_found"] == [manual + 1]
    assert {p["ticket"]: p["opened_by"] for p in w.engine.positions()} == {manual: "externo", manual + 1: "auto"}
    assert w.engine._auto_ids() == frozenset({manual + 1})
    refused = w.engine.auto_close(manual, "salida por regla de la estrategia")
    assert refused["closed"] is False and any("no la abrió el modo automático" in x for x in refused["reasons"])


def test_unanswered_auto_order_with_a_terminal_that_still_does_not_answer(world, monkeypatch):
    world.fake.send_none = True
    orig = world.fake.order_send

    def send_and_drop(request):
        out = orig(request)
        world.fake.fail_positions = True  # tras el envío, el terminal tampoco entrega las posiciones
        return out

    monkeypatch.setattr(world.fake, "order_send", send_and_drop)
    r = world.buy()
    assert r["sent"] is False and r["status"] == "unknown" and r["position_found"] == [] and "journal_error" not in r
    assert r["next"].startswith("resultado desconocido: verifica con positions()") and "anotada" not in r["next"]
    assert world.events() == ["auto_order_attempt", "auto_order_unknown"]
    assert not (world.root / "journal" / "auto_positions.jsonl").exists()


def test_unanswered_auto_order_reports_a_registry_that_cannot_be_written(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    (tmp_path / "journal" / "auto_positions.jsonl").mkdir(parents=True)  # el registro no se puede escribir
    w.fake.send_none = True
    r = w.buy()
    assert r["status"] == "unknown" and r["position_found"] == [1000] and "registro de posiciones del bot" in r["journal_error"]
    assert "no abras otra" in r["next"] and "anotada como del bot" not in r["next"]  # no se afirma lo que no se pudo hacer
    assert "ticket 1000" in r["next"] and "no se pudo anotar" in r["next"]


def test_auto_close_and_tighten_never_depend_on_the_journal(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    assert w.buy()["sent"]
    t = w.fake._positions[-1].ticket
    w.engine = w.new_engine(journal=FailingJournal(tmp_path / "journal" / "trades.jsonl"))
    m = w.engine.auto_tighten_stop(t, 1.0990, "mover a breakeven tras avanzar 1R")
    assert m["modified"] is True and "No space left" in m["journal_error"] and w.fake._positions[-1].sl == 1.099
    c = w.engine.auto_close(t, "cierre: la vela H1 cerró bajo la EMA50")
    assert c["closed"] is True and c["status"] == "filled" and "No space left" in c["journal_error"]
    assert w.engine.positions() == [] and len(w.fake.sent) == 3


def test_auto_close_and_tighten_without_answer_are_unknown(world):
    assert world.buy()["sent"]
    t = world.fake._positions[-1].ticket
    world.fake.send_none = True
    m = world.engine.auto_tighten_stop(t, 1.0990, "mover a breakeven tras avanzar 1R")
    assert m["modified"] is False and m["status"] == "unknown" and "positions()" in m["error"]
    c = world.engine.auto_close(t, "cierre: la vela H1 cerró bajo la EMA50")
    assert c["closed"] is False and c["status"] == "unknown" and "positions()" in c["error"]
    assert world.events()[-2:] == ["auto_modify_unknown", "auto_close_unknown"]
    assert "EMA50" in world.engine.journal_tail(1)[0]["reason"]


def test_halt_close_all_with_a_failing_journal_closes_bot_and_manual_positions(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    assert w.buy()["sent"]
    w.fake.add_position("GBPUSD", "sell", 0.1, 1.3010, sl=1.3060, magic=w.cfg.magic)
    w.engine = w.new_engine(journal=FailingJournal(tmp_path / "journal" / "trades.jsonl"))
    res = w.engine.halt_trading("parada con el disco lleno", close_all=True, confirm=True)
    assert res["halted"] is True and (tmp_path / "HALT").exists() and res["errors"] == []
    assert len(res["closed"]) == 2 and all(c["closed"] for c in res["closed"]) and "No space left" in res["journal_error"]
    assert w.engine.positions() == []
    st = w.engine.auto_status()
    assert st["can_open"] is False and st["can_manage_positions"] is False


# ---- los límites de [auto] se aplican con el valor escrito, no con el de por defecto -----------------


def test_auto_order_applies_the_min_rr_written_in_the_config(tmp_path, monkeypatch):
    strict = World(tmp_path / "estricto", monkeypatch, auto=AutoConfig(enabled=True, min_rr=3.0))
    r = strict.buy()  # 20 pips de riesgo y 40 de beneficio: 2,0 pasa con el 1,5 por defecto, no con 3,0
    assert not r["sent"] and r["reasons"] == ["ratio beneficio/riesgo 2.00 menor que el mínimo 3.0"]
    assert not strict.fake.sent and not strict.fake.checked
    assert strict.engine.auto_status()["limits"]["min_rr"] == 3.0
    assert strict.buy(tp=1.1061)["sent"]  # 60 pips de beneficio: 3,0, justo el mínimo
    loose = World(tmp_path / "laxo", monkeypatch, auto=AutoConfig(enabled=True, min_rr=1.0))
    assert loose.buy(tp=1.1021)["sent"]  # 1,0: con el 1,5 por defecto se rechazaría
    default = World(tmp_path / "defecto", monkeypatch)
    assert default.buy(tp=1.1031)["sent"]  # 30 pips: 1,5 justo, aunque la resta en coma flotante dé 1,4999999999999


def test_auto_order_applies_one_position_per_symbol_from_the_config(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, auto=AutoConfig(enabled=True, cooldown_minutes=0, one_position_per_symbol=False))
    assert w.buy("EURUSD")["sent"]
    w.advance(1)
    assert w.buy("EURUSD")["sent"] and len(w.fake.sent) == 2  # con el interruptor en false caben dos en el mismo símbolo
    assert w.engine.auto_status()["limits"]["one_position_per_symbol"] is False


def test_auto_order_applies_the_quote_age_written_in_the_config(tmp_path, monkeypatch):
    cfg20 = RiskConfig(auto=AutoConfig(enabled=True, max_quote_age_seconds=20))
    assert rules.symbol_entry_blockers(cfg20, "EURUSD", [], 20.0) == []  # justo en el máximo todavía vale
    assert any("máximo 20 s" in r for r in rules.symbol_entry_blockers(cfg20, "EURUSD", [], 20.5))

    fast = World(tmp_path / "exigente", monkeypatch, auto=AutoConfig(enabled=True, max_quote_age_seconds=20))
    fast.engine.auto_status()  # fija la hora del servidor con el mercado abierto
    fast.fake.freeze_ticks("EURUSD")  # EURUSD deja de cotizar; GBPUSD sigue
    fast.t += 30  # 30 s: valdría con los 60 s por defecto, no con 20
    r = fast.buy("EURUSD")
    assert not r["sent"] and len(r["reasons"]) == 1 and "cotización de hace 30 s (máximo 20 s)" in r["reasons"][0] and not fast.fake.sent
    assert fast.engine.auto_status()["can_open"] is True  # GBPUSD cotiza: el bloqueo es solo de EURUSD
    fast.fake.freeze_ticks()  # ahora se para también GBPUSD
    fast.t += 30
    st = fast.engine.auto_status()
    assert st["can_open"] is False and len(st["blockers"]) == 1
    assert "sin cotizaciones recientes" in st["blockers"][0] and "hace 30 s (máximo 20 s)" in st["blockers"][0]

    slow = World(tmp_path / "tolerante", monkeypatch, auto=AutoConfig(enabled=True, max_quote_age_seconds=300))
    slow.engine.auto_status()
    slow.fake.freeze_ticks("EURUSD")
    slow.t += 200  # 200 s: con los 60 s por defecto se rechazaría; el usuario escribió 300
    assert slow.buy("EURUSD")["sent"]


# ---- cuenta real: hacen falta los dos permisos ----------------------------------------------------------


@pytest.mark.parametrize("trade_mode, name", [(2, "REAL"), (1, "CONTEST")])
def test_auto_mode_outside_demo_needs_the_general_permission_too(tmp_path, monkeypatch, trade_mode, name):
    # La otra mitad del doble permiso: [auto] allow_live = true con allow_live = false general no basta.
    only_auto = RiskConfig(allow_live=False, auto=AutoConfig(enabled=True, allow_live=True))
    not_demo = AccountSnapshot(login=2, trade_mode=trade_mode, balance=10_000, equity=10_000, currency="USD")
    assert any("solo opera en DEMO" in r for r in rules.mode_blockers(only_auto, not_demo, False))
    w = World(tmp_path, monkeypatch, allow_live=False, auto=AutoConfig(enabled=True, allow_live=True))
    w.fake.set_account(trade_mode=trade_mode)
    bot = w.fake.add_position("EURUSD", "buy", 0.1, 1.0990, sl=1.0970, tp=1.1100, magic=w.cfg.auto_magic)
    closed = w.engine.auto_close(bot, "salida por regla de la estrategia")
    moved = w.engine.auto_tighten_stop(bot, 1.0980, "proteger tras avanzar 1R")
    opened = w.buy("GBPUSD")
    for res, key in ((closed, "closed"), (moved, "modified"), (opened, "sent")):
        about_account = [x for x in res["reasons"] if name in x]
        assert res[key] is False and len(about_account) == 1 and "solo opera en DEMO" in about_account[0], res
    assert not w.fake.sent and not w.fake.checked and w.fake._positions[-1].sl == 1.0970
    st = w.engine.auto_status()
    assert st["can_open"] is False and st["can_manage_positions"] is False and st["account_mode"] == name
    assert len([b for b in st["blockers"] if name in b]) == 1  # el motivo de la cuenta sale una vez, no dos


# ---- auto_status dice lo mismo que el motor de riesgo ---------------------------------------------------


def test_auto_status_shows_the_general_risk_blockers(tmp_path, monkeypatch):
    lossy = World(tmp_path / "perdida", monkeypatch)
    bot = lossy.fake.add_position("GBPUSD", "buy", 0.1, 1.2990, sl=1.2970, tp=1.3100, magic=lossy.cfg.auto_magic)
    lossy.fake.add_deal(profit=-300.0)
    lossy.fake.set_account(balance=9_700.0, equity=9_700.0)
    st = lossy.engine.auto_status()
    assert st["can_open"] is False and len(st["blockers"]) == 1 and "límite de pérdida diaria alcanzado" in st["blockers"][0]
    assert st["can_manage_positions"] is True  # con el límite diario alcanzado el bot no abre, pero sigue cuidando lo suyo
    assert lossy.engine.auto_tighten_stop(bot, 1.2980, "mover a breakeven tras avanzar 1R")["modified"] is True

    off = World(tmp_path / "algo", monkeypatch)
    off.fake.set_terminal(trade_allowed=False)
    st = off.engine.auto_status()
    assert st["can_open"] is False and st["terminal_can_trade"] is False
    assert len(st["blockers"]) == 1 and "AutoTrading está desactivado" in st["blockers"][0]

    full = World(tmp_path / "lleno", monkeypatch, max_open_positions=1)
    full.fake.add_position("GBPUSD", "sell", 0.1, 1.3010, sl=1.3060, magic=full.cfg.magic)  # una manual ya ocupa el único hueco
    st = full.engine.auto_status()
    assert st["can_open"] is False and len(st["blockers"]) == 1 and "1 posiciones abiertas (máximo 1)" in st["blockers"][0]
    assert st["terminal_can_trade"] is True and st["open_auto_positions"] == []


def test_daily_loss_limit_counts_commission_swap_and_fee(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    # 280 de pérdida por precio: no llega al 3 % de 10.000. Con comisión, swap y tasa son 300,50 y sí llega.
    w.fake.add_deal(profit=-280.0, commission=-6.0, swap=-10.5, fee=-4.0)
    w.fake.set_account(balance=9_699.5, equity=9_699.5)
    rs = w.engine.risk_status()
    assert rs["realized_today"] == -300.5 and rs["daily_loss_limit"] == -300.0 and rs["daily_loss_hit"] is True
    st = w.engine.auto_status()
    assert st["can_open"] is False and any("límite de pérdida diaria alcanzado" in b for b in st["blockers"])
    r = w.buy()
    assert not r["sent"] and any("límite de pérdida diaria alcanzado" in x for x in r["reasons"]) and not w.fake.sent


# ---- una posición del bot que pasa la medianoche --------------------------------------------------------


def test_bot_position_that_crosses_midnight_keeps_the_cooldown_and_counts_in_todays_streak(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, auto=AutoConfig(enabled=True, cooldown_minutes=30, max_consecutive_losses=1))
    w.advance(11 * 60 + 50)  # 23:50 UTC
    assert w.buy("EURUSD")["sent"]
    w.advance(20)  # 00:10 UTC del día siguiente: cupo nuevo, pero el enfriamiento sigue contando desde la entrada de ayer
    st = w.engine.auto_status()
    assert st["trades_today"] == 0 and st["cooldown_remaining_minutes"] == 10.0 and st["can_open"] is False
    r = w.buy("GBPUSD")
    assert not r["sent"] and any("enfriamiento: faltan 10 min" in x for x in r["reasons"]) and len(w.fake.sent) == 1
    w.fake.sltp_exit_magic = 0  # su SL salta de madrugada y el deal de salida no conserva el magic del bot
    w.fake.hit_stop(w.fake._positions[-1].ticket, profit=-50.0)
    w.fake.set_account(balance=9_950.0, equity=9_950.0)
    w.advance(15)  # 00:25 UTC: el enfriamiento ya pasó; lo que bloquea ahora es la pérdida de hoy
    st = w.engine.auto_status()
    assert st["trades_today"] == 0 and st["cooldown_remaining_minutes"] == 0.0
    assert st["closed_today"] == 1 and st["consecutive_losses"] == 1
    r = w.buy("GBPUSD")
    assert not r["sent"] and len(r["reasons"]) == 1 and "1 pérdidas seguidas hoy (máximo 1)" in r["reasons"][0] and len(w.fake.sent) == 1


# ---- cotización parada y referencias del reloj ----------------------------------------------------------


def test_quote_and_bars_mark_the_quote_stale_when_its_age_is_unknown_or_over_five_minutes(tmp_path, monkeypatch):
    closes = [1.1 + i * 0.0001 for i in range(60)]
    shut = World(tmp_path / "cerrado", monkeypatch)
    shut.fake.freeze_ticks()  # el proceso arranca con el mercado cerrado: no llega a conocer la hora del servidor
    shut.fake.set_rates("EURUSD", shut.fake.TIMEFRAME_M15, closes)
    q = shut.engine.quote("EURUSD")
    assert q["age_seconds"] is None and q["stale"] is True  # sin poder medir la antigüedad, la cotización no se da por buena
    b = shut.engine.bars("EURUSD", "M15", 5)
    assert b["server_clock_known"] is False and b["quote"]["age_seconds"] is None and b["quote"]["stale"] is True

    w = World(tmp_path / "abierto", monkeypatch)
    w.fake.set_rates("EURUSD", w.fake.TIMEFRAME_M15, closes)
    q = w.engine.quote("EURUSD")
    assert q["age_seconds"] == 0 and q["stale"] is False
    w.fake.freeze_ticks("EURUSD")  # EURUSD deja de cotizar; GBPUSD mantiene la hora del servidor
    last = w.fake.tick_time("EURUSD")
    seen = {}
    for age in (300, 301):
        w.t = last + age
        q, b = w.engine.quote("EURUSD"), w.engine.bars("EURUSD", "M15", 5)
        seen[age] = (q["age_seconds"], q["stale"], b["quote"]["stale"])
    assert seen == {300: (300, False, False), 301: (301, True, True)}  # parada a partir de más de 5 minutos


def test_server_clock_also_reads_the_requested_symbol_and_the_symbols_with_positions(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, symbols=("EURUSD",))
    w.fake.freeze_ticks("EURUSD")  # el único símbolo de la lista no cotiza; GBPUSD, fuera de la lista, sí
    assert w.engine.auto_status()["server_clock_known"] is False
    q = w.new_engine().quote("GBPUSD")  # proceso nuevo: la hora sale del símbolo que se consulta
    assert q["age_seconds"] == 0 and q["stale"] is False
    bot = w.fake.add_position("GBPUSD", "buy", 0.1, 1.2990, sl=1.2970, tp=1.3100, magic=w.cfg.auto_magic)
    st = w.new_engine().auto_status()  # y de los símbolos con posición abierta, aunque ya no estén en la lista
    assert st["server_clock_known"] is True and st["can_manage_positions"] is True
    assert [p["ticket"] for p in st["open_auto_positions"]] == [bot]


# ---- auto_tighten_stop con el mismo stop ----------------------------------------------------------------


def test_auto_tighten_stop_with_the_same_stop_sends_nothing(world):
    assert world.engine.auto_order("EURUSD", "sell", 1.1020, 1.0960, WHY)["sent"]
    sell = world.fake._positions[-1].ticket
    world.advance(16)
    assert world.buy("GBPUSD")["sent"]  # compra con SL 1.2981
    buy = world.fake._positions[-1].ticket
    sent_before = len(world.fake.sent)
    for ticket, same in ((sell, 1.1020), (sell, 1.102004), (buy, 1.2981)):  # 1.102004 es 1.10200 en la rejilla del símbolo
        r = world.engine.auto_tighten_stop(ticket, same, "mover el stop a favor tras nuevo extremo")
        assert r["modified"] is False and len(r["reasons"]) == 1 and "solo se permite acercar" in r["reasons"][0], (ticket, same, r)
    assert len(world.fake.sent) == sent_before and world.events()[-3:] == ["auto_modify_rejected"] * 3
    assert [p.sl for p in world.fake._positions] == [1.102, 1.2981]


@pytest.mark.parametrize("wipe", [True, False])
@pytest.mark.parametrize("theirs", [
    {"magic": 0, "sl": 1.0981},          # abierta a mano en el terminal, con el mismo stop por casualidad
    {"magic": 0, "sl": 1.0970},          # abierta a mano, con otro stop
    {"magic": 20261023, "sl": 1.0981},   # abierta por el usuario desde una sesión normal de Claude (magic manual)
    {"magic": 424242, "sl": 1.0981},     # abierta por otro programa del terminal
])
def test_unanswered_auto_order_never_adopts_a_position_opened_by_someone_else_at_the_same_moment(tmp_path, monkeypatch, wipe, theirs):
    # Mientras order_send se queda sin respuesta, otra posición del mismo símbolo y lado aparece en el terminal. Solo la
    # del bot puede quedar anotada: la otra no lleva su stop o no lleva su magic. Con un broker que conserva el magic
    # basta el magic; con uno que lo borra, el stop pedido. Si una posición ajena coincide en las dos cosas (magic 0 y
    # el mismo stop al punto, con el broker borrando el magic), no hay forma de distinguirla: es el único caso que queda.
    w = World(tmp_path, monkeypatch, auto=AutoConfig(enabled=True, one_position_per_symbol=False))
    w.fake.wipe_magic = wipe
    w.fake.send_none = True
    original = w.fake.order_send
    mine = []

    def send_and_someone_else_opens(request):
        out = original(request)
        mine.append(w.fake._positions[-1].ticket)
        mine.append(w.fake.add_position("EURUSD", "buy", 0.2, 1.0995, sl=theirs["sl"], magic=theirs["magic"]))
        return out

    w.fake.order_send = send_and_someone_else_opens
    r = w.buy("EURUSD")
    bot, other = mine
    assert r["status"] == "unknown"
    ambiguous = wipe and theirs["magic"] == 0 and theirs["sl"] == 1.0981
    assert r["position_found"] == ([bot, other] if ambiguous else [bot])
    if ambiguous:
        return
    assert w.engine.positions()[-1]["opened_by"] != "auto" and other not in w.engine._auto_ids()
    refused = w.engine.auto_close(other, "salida por regla de la estrategia")
    assert refused["closed"] is False and any(t.ticket == other for t in w.fake._positions)


def test_pc_clock_fast_is_still_named_after_a_minute_without_readings(tmp_path, monkeypatch):
    # El reloj de Windows va 90 s adelantado y el modelo razona 65 s entre bars y auto_order: la lectura anterior ya no
    # es reciente, pero el tick se vio cambiar después de ella y parece más viejo que el tiempo transcurrido desde
    # entonces. Eso solo lo explican un reloj adelantado o una reconexión, no un mercado parado.
    w = World(tmp_path, monkeypatch, offset=3 * 3600)
    skew = 90
    monkeypatch.setattr(w.engine, "_now", lambda: datetime.fromtimestamp(w.t + skew, tz=timezone.utc))
    assert w.engine.auto_status()["server_clock_known"] is True
    w.engine.quote("EURUSD")  # la última lectura antes de que el modelo se ponga a razonar
    w.t += 65
    w.fake.touch_ticks()  # siguen llegando cotizaciones
    reason = w.buy("EURUSD")["reasons"][0]
    assert "aunque acaba de llegar" in reason and "mercado parado o sin ticks" not in reason
    st = w.engine.auto_status()
    assert st["blockers"][0].startswith("llegan cotizaciones de ") and "mercado parado o sin ticks" not in st["blockers"][0]


def test_really_stopped_market_after_a_minute_without_readings_is_still_called_stopped(world):
    # Reloj del PC en hora: el mercado se para justo después de una lectura y la siguiente llega 70 s más tarde. El
    # tick cambió entre las dos, pero parece tan viejo como el tiempo que pasó: es un mercado parado.
    assert world.engine.auto_status()["can_open"] is True
    world.t += 5
    world.fake.touch_ticks()
    world.fake.freeze_ticks()
    world.t += 65
    reason = world.buy("EURUSD")["reasons"][0]
    assert reason.endswith("mercado parado o sin ticks") and "acaba de llegar" not in reason


def test_a_stop_change_the_broker_refuses_says_failed(world):
    t = world.fake.add_position("EURUSD", "buy", 0.1, 1.0990, sl=1.0970, tp=1.1041, magic=world.cfg.auto_magic)
    world.fake.send_retcode = 10016  # INVALID_STOPS
    r = world.engine.auto_tighten_stop(t, 1.0980, "proteger: el precio avanzó 1R a favor")
    assert r["modified"] is False and r["status"] == "failed" and r["retcode_name"] == "INVALID_STOPS"
    world.fake.send_retcode = 10009
    ok = world.engine.auto_tighten_stop(t, 1.0980, "proteger: el precio avanzó 1R a favor")
    assert ok["modified"] is True and ok["status"] == "done"
