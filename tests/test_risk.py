from datetime import datetime, timezone

import pytest

from mt5_mcp.config import RiskConfig
from mt5_mcp.risk import (
    ACCOUNT_TRADE_MODE_REAL,
    ORDER_FILLING_FOK,
    ORDER_FILLING_IOC,
    ORDER_FILLING_RETURN,
    SYMBOL_TRADE_MODE_CLOSEONLY,
    SYMBOL_TRADE_MODE_LONGONLY,
    AccountSnapshot,
    OrderIntent,
    RiskError,
    RiskState,
    SymbolSpec,
    daily_loss_hit,
    evaluate_order,
    in_trading_hours,
    loss_per_lot,
    normalize_side,
    pick_filling,
    round_volume_down,
    size_for_risk,
)

EURUSD = SymbolSpec(name="EURUSD", digits=5, point=0.00001, tick_size=0.00001, tick_value=1.0, volume_min=0.01, volume_max=100, volume_step=0.01, stops_level=0, spread=10, filling_mode=1)
XAUUSD = SymbolSpec(name="XAUUSD", digits=2, point=0.01, tick_size=0.01, tick_value=1.0, volume_min=0.01, volume_max=50, volume_step=0.01, stops_level=50, spread=30, filling_mode=2)
DEMO = AccountSnapshot(login=1, trade_mode=0, balance=10_000, equity=10_000, currency="USD")
REAL = AccountSnapshot(login=2, trade_mode=ACCOUNT_TRADE_MODE_REAL, balance=10_000, equity=10_000, currency="USD")
CFG = RiskConfig(symbols=("EURUSD", "XAUUSD"), max_volume_per_order=1.0, max_total_volume=2.0)
STATE = RiskState()


def test_round_volume_down():
    assert round_volume_down(EURUSD, 0.4999) == 0.49
    assert round_volume_down(EURUSD, 0.5) == 0.5
    assert round_volume_down(SymbolSpec("X", 2, 0.01, 0.01, 1, 0.1, 10, 0.1), 0.37) == 0.3
    assert round_volume_down(EURUSD, 0.0) == 0.0


def test_loss_per_lot_eurusd_20_pips():
    # 20 pips = 200 puntos; 1 USD por punto por lote => 200 USD por lote
    assert loss_per_lot(EURUSD, 1.1000, 1.0980) == pytest.approx(200.0)


def test_size_for_risk_one_percent():
    vol, risk, lpl = size_for_risk(CFG, EURUSD, 10_000, 1.1000, 1.0980)
    assert risk == 100.0
    assert lpl == pytest.approx(200.0)
    assert vol == 0.5


def test_size_for_risk_respects_caps_and_step():
    cfg = RiskConfig(max_volume_per_order=0.1)
    vol, _, _ = size_for_risk(cfg, EURUSD, 10_000, 1.1000, 1.0980)
    assert vol == 0.1
    # riesgo pedido mayor al máximo se recorta al máximo configurado
    vol2, risk2, _ = size_for_risk(CFG, EURUSD, 10_000, 1.1000, 1.0980, risk_pct=5.0)
    assert risk2 == 100.0 and vol2 == 0.5
    # rounding hacia abajo: 0.333.. lotes -> 0.33
    vol3, _, _ = size_for_risk(CFG, EURUSD, 10_000, 1.1000, 1.0970)
    assert vol3 == 0.33


def test_sl_equal_to_price_is_error():
    with pytest.raises(RiskError):
        loss_per_lot(EURUSD, 1.1, 1.1)


def test_normalize_side():
    assert normalize_side("BUY") == "buy" and normalize_side("venta") == "sell"
    with pytest.raises(RiskError):
        normalize_side("hold")


def test_pick_filling():
    assert pick_filling(EURUSD) == ORDER_FILLING_FOK
    assert pick_filling(XAUUSD) == ORDER_FILLING_IOC
    assert pick_filling(SymbolSpec("X", 5, 1e-5, 1e-5, 1, 0.01, 1, 0.01, filling_mode=0)) == ORDER_FILLING_RETURN


def test_trading_hours():
    cfg = RiskConfig(trading_hours_utc=(7, 20))
    assert in_trading_hours(cfg, datetime(2026, 1, 1, 12, tzinfo=timezone.utc))
    assert not in_trading_hours(cfg, datetime(2026, 1, 1, 22, tzinfo=timezone.utc))
    wrap = RiskConfig(trading_hours_utc=(22, 4))
    assert in_trading_hours(wrap, datetime(2026, 1, 1, 23, tzinfo=timezone.utc))
    assert in_trading_hours(wrap, datetime(2026, 1, 1, 3, tzinfo=timezone.utc))
    assert not in_trading_hours(wrap, datetime(2026, 1, 1, 12, tzinfo=timezone.utc))
    assert in_trading_hours(RiskConfig(), None)


def test_daily_loss_hit_uses_day_start_balance():
    # balance ya refleja -250 realizados; balance inicial del día = 10_000; límite 3% = -300
    acct = AccountSnapshot(login=1, trade_mode=0, balance=9_750, equity=9_700, currency="USD")
    st = RiskState(realized_today=-250, floating_pnl=-40)
    assert not daily_loss_hit(CFG, acct, st)
    st2 = RiskState(realized_today=-250, floating_pnl=-60)
    assert daily_loss_hit(CFG, acct, st2)


def _ev(intent, acct=DEMO, spec=EURUSD, state=STATE, price=1.1001, cfg=CFG):
    return evaluate_order(cfg, acct, spec, state, intent, price)


def test_evaluate_ok_sizes_volume():
    ev = _ev(OrderIntent("EURUSD", "buy", sl=1.0981, tp=1.1041))
    assert ev.ok, ev.reasons
    assert ev.volume == 0.5
    assert ev.risk_pct == pytest.approx(1.0)
    assert ev.sl_points == 200
    assert not ev.warnings


def test_evaluate_rejects_live_without_allow():
    ev = _ev(OrderIntent("EURUSD", "buy", sl=1.0981), acct=REAL)
    assert not ev.ok and any("REAL" in r for r in ev.reasons)
    ev2 = evaluate_order(RiskConfig(allow_live=True), REAL, EURUSD, STATE, OrderIntent("EURUSD", "buy", sl=1.0981), 1.1001)
    assert ev2.ok


def test_evaluate_requires_sl_and_correct_side():
    assert not _ev(OrderIntent("EURUSD", "buy", sl=0)).ok
    ev = _ev(OrderIntent("EURUSD", "buy", sl=1.1050))
    assert any("por debajo" in r for r in ev.reasons)
    ev = _ev(OrderIntent("EURUSD", "sell", sl=1.0950), price=1.1000)
    assert any("por encima" in r for r in ev.reasons)
    ev = _ev(OrderIntent("EURUSD", "buy", sl=1.0981, tp=1.0990))
    assert any("TP" in r for r in ev.reasons)


def test_evaluate_respects_stops_level_and_min_sl():
    ev = _ev(OrderIntent("XAUUSD", "buy", sl=2000.20), spec=XAUUSD, price=2000.50)
    assert any("demasiado cerca" in r for r in ev.reasons)
    cfg = RiskConfig(symbols=("EURUSD",), min_sl_points=300)
    ev2 = _ev(OrderIntent("EURUSD", "buy", sl=1.0981), cfg=cfg)
    assert any("mínimo 300" in r for r in ev2.reasons)


def test_evaluate_rejects_symbol_outside_allowlist_and_broker_mode():
    ev = _ev(OrderIntent("GBPUSD", "buy", sl=1.0981), spec=EURUSD._replace(name="GBPUSD") if hasattr(EURUSD, "_replace") else EURUSD)
    spec = SymbolSpec(**{**EURUSD.__dict__, "name": "GBPUSD"})
    ev = _ev(OrderIntent("GBPUSD", "buy", sl=1.0981), spec=spec)
    assert any("lista blanca" in r for r in ev.reasons)
    closeonly = SymbolSpec(**{**EURUSD.__dict__, "trade_mode": SYMBOL_TRADE_MODE_CLOSEONLY})
    assert any("no permite abrir" in r for r in _ev(OrderIntent("EURUSD", "buy", sl=1.0981), spec=closeonly).reasons)
    longonly = SymbolSpec(**{**EURUSD.__dict__, "trade_mode": SYMBOL_TRADE_MODE_LONGONLY})
    assert any("long only" in r for r in _ev(OrderIntent("EURUSD", "sell", sl=1.1021), spec=longonly).reasons)


def test_evaluate_rejects_halt_autotrading_daily_loss_positions():
    assert any("kill switch" in r for r in _ev(OrderIntent("EURUSD", "buy", sl=1.0981), state=RiskState(halted=True)).reasons)
    assert any("AutoTrading" in r for r in _ev(OrderIntent("EURUSD", "buy", sl=1.0981), state=RiskState(trade_allowed=False)).reasons)
    st = RiskState(realized_today=-310)
    acct = AccountSnapshot(login=1, trade_mode=0, balance=9_690, equity=9_690, currency="USD")
    assert any("pérdida diaria" in r for r in _ev(OrderIntent("EURUSD", "buy", sl=1.0981), acct=acct, state=st).reasons)
    assert any("posiciones abiertas" in r for r in _ev(OrderIntent("EURUSD", "buy", sl=1.0981), state=RiskState(open_positions=3)).reasons)


def test_evaluate_volume_caps():
    ev = _ev(OrderIntent("EURUSD", "buy", sl=1.0981, volume=0.6))
    assert any("supera el máximo permitido" in r for r in ev.reasons)
    ev2 = _ev(OrderIntent("EURUSD", "buy", sl=1.0981, volume=0.2))
    assert ev2.ok and ev2.volume == 0.2 and ev2.risk_amount == pytest.approx(40.0)
    ev3 = _ev(OrderIntent("EURUSD", "buy", sl=1.0981, volume=0.5), state=RiskState(total_volume=1.8))
    assert any("max_total_volume" in r for r in ev3.reasons)


def test_evaluate_rejects_when_budget_below_min_lot():
    small = AccountSnapshot(login=1, trade_mode=0, balance=100, equity=100, currency="USD")
    ev = _ev(OrderIntent("EURUSD", "buy", sl=1.0981), acct=small)  # riesgo 1 USD; lote mínimo pierde 2 USD
    assert not ev.ok and any("lote mínimo" in r for r in ev.reasons)


def test_evaluate_spread_and_rr_warnings():
    cfg = RiskConfig(symbols=("EURUSD",), max_spread_points=5)
    assert any("spread" in r for r in _ev(OrderIntent("EURUSD", "buy", sl=1.0981), cfg=cfg).reasons)
    ev = _ev(OrderIntent("EURUSD", "buy", sl=1.0981, tp=1.1011))
    assert ev.ok and any("beneficio/riesgo" in w for w in ev.warnings)
    wide = SymbolSpec(**{**EURUSD.__dict__, "spread": 80})
    ev2 = _ev(OrderIntent("EURUSD", "buy", sl=1.0981), spec=wide)
    assert any("un tercio" in w for w in ev2.warnings)


def test_evaluate_rejects_zero_price():
    ev = _ev(OrderIntent("EURUSD", "buy", sl=1.0981), price=0.0)
    assert not ev.ok and any("precio" in r for r in ev.reasons)
