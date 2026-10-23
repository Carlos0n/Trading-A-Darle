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
    day_start_balance,
    evaluate_order,
    in_trading_hours,
    loss_per_lot,
    normalize_price,
    normalize_side,
    pick_filling,
    round_volume_down,
    size_for_risk,
    symbol_allowed,
    trading_blockers,
)

EURUSD = SymbolSpec(name="EURUSD", digits=5, point=0.00001, tick_size=0.00001, tick_value=1.0, volume_min=0.01, volume_max=100, volume_step=0.01, stops_level=0, spread=10, filling_mode=1)
XAUUSD = SymbolSpec(name="XAUUSD", digits=2, point=0.01, tick_size=0.01, tick_value=1.0, volume_min=0.01, volume_max=50, volume_step=0.01, stops_level=50, spread=30, filling_mode=2)
US500 = SymbolSpec(name="US500", digits=2, point=0.01, tick_size=0.25, tick_value=0.25, volume_min=0.1, volume_max=50, volume_step=0.1, stops_level=0, spread=50, filling_mode=2)
DEMO = AccountSnapshot(login=1, trade_mode=0, balance=10_000, equity=10_000, currency="USD")
REAL = AccountSnapshot(login=2, trade_mode=ACCOUNT_TRADE_MODE_REAL, balance=10_000, equity=10_000, currency="USD")
CFG = RiskConfig(symbols=("EURUSD", "XAUUSD", "US500"), max_volume_per_order=1.0, max_total_volume=2.0)
STATE = RiskState()


def spec_with(base: SymbolSpec, **changes) -> SymbolSpec:
    return SymbolSpec(**{**base.__dict__, **changes})


def _ev(intent, acct=DEMO, spec=EURUSD, state=STATE, bid=1.1000, ask=1.1001, cfg=CFG):
    return evaluate_order(cfg, acct, spec, state, intent, bid, ask)


# ---- utilidades -------------------------------------------------------------------------------


def test_round_volume_down_decimal_exact():
    assert round_volume_down(EURUSD, 0.4999) == 0.49
    assert round_volume_down(EURUSD, 0.5) == 0.5
    assert round_volume_down(EURUSD, 0.30000000000000004) == 0.3
    assert round_volume_down(EURUSD, 0.0) == 0.0
    assert round_volume_down(EURUSD, float("nan")) == 0.0
    step01 = spec_with(EURUSD, volume_step=0.1)
    assert round_volume_down(step01, 0.37) == 0.3
    step025 = spec_with(EURUSD, volume_step=0.25)
    assert round_volume_down(step025, 0.80) == 0.75
    assert round_volume_down(step025, 0.75) == 0.75
    assert round_volume_down(step025, 0.74) == 0.5
    step0125 = spec_with(EURUSD, volume_step=0.125)
    assert round_volume_down(step0125, 0.3) == 0.25


def test_normalize_price_to_tick_grid():
    assert normalize_price(US500, 4990.30) == 4990.25
    assert normalize_price(US500, 5010.40) == 5010.50
    assert normalize_price(EURUSD, 1.0981499999) == 1.09815
    assert normalize_price(spec_with(EURUSD, tick_size=0.0), 1.098149999) == 1.09815


def test_loss_per_lot_eurusd_20_pips():
    # 20 pips = 200 puntos; 1 USD por punto por lote => 200 USD por lote
    assert loss_per_lot(EURUSD, 1.1000, 1.0980) == pytest.approx(200.0)


def test_loss_per_lot_uses_loss_tick_value_when_larger():
    spec = spec_with(EURUSD, tick_value=0.90, tick_value_loss=0.95)
    assert loss_per_lot(spec, 1.1000, 1.0980) == pytest.approx(200 * 0.95)
    spec2 = spec_with(EURUSD, tick_value=1.0, tick_value_loss=0.0)
    assert loss_per_lot(spec2, 1.1000, 1.0980) == pytest.approx(200.0)


def test_size_for_risk_one_percent():
    vol, risk, lpl = size_for_risk(CFG, EURUSD, 10_000, 1.1000, 1.0980)
    assert risk == 100.0 and lpl == pytest.approx(200.0) and vol == 0.5


def test_size_for_risk_respects_caps_and_step():
    cfg = RiskConfig(max_volume_per_order=0.1)
    assert size_for_risk(cfg, EURUSD, 10_000, 1.1000, 1.0980)[0] == 0.1
    vol2, risk2, _ = size_for_risk(CFG, EURUSD, 10_000, 1.1000, 1.0980, risk_pct=5.0)
    assert risk2 == 100.0 and vol2 == 0.5  # riesgo pedido mayor al máximo se recorta
    assert size_for_risk(CFG, EURUSD, 10_000, 1.1000, 1.0970)[0] == 0.33  # 0.333.. hacia abajo


def test_risk_pct_below_max_is_honored():
    vol, risk, _ = size_for_risk(CFG, EURUSD, 10_000, 1.1000, 1.0980, risk_pct=0.5)
    assert risk == 50.0 and vol == 0.25
    ev = _ev(OrderIntent("EURUSD", "buy", sl=1.0981, risk_pct=0.5))
    assert ev.ok and ev.volume == 0.25 and ev.risk_pct == pytest.approx(0.5)


def test_size_for_risk_invalid_inputs():
    with pytest.raises(RiskError):
        loss_per_lot(EURUSD, 1.1, 1.1)
    with pytest.raises(RiskError):
        size_for_risk(CFG, EURUSD, 10_000, 1.1000, 1.0980, risk_pct=0)
    with pytest.raises(RiskError):
        size_for_risk(CFG, EURUSD, 10_000, 1.1000, 1.0980, risk_pct=float("nan"))
    with pytest.raises(RiskError):
        size_for_risk(CFG, EURUSD, 0, 1.1000, 1.0980)
    with pytest.raises(RiskError):
        size_for_risk(CFG, EURUSD, -5, 1.1000, 1.0980)


def test_normalize_side():
    assert normalize_side("BUY") == "buy" and normalize_side("venta") == "sell"
    with pytest.raises(RiskError):
        normalize_side("hold")


def test_symbol_allowed_is_case_insensitive_and_keeps_suffix():
    cfg = RiskConfig(symbols=("EURUSD.m",))
    assert symbol_allowed(cfg, "EURUSD.m") and symbol_allowed(cfg, "eurusd.M")
    assert not symbol_allowed(cfg, "EURUSD")
    assert symbol_allowed(RiskConfig(), "ANYTHING")


def test_pick_filling():
    assert pick_filling(EURUSD) == ORDER_FILLING_FOK
    assert pick_filling(XAUUSD) == ORDER_FILLING_IOC
    assert pick_filling(spec_with(EURUSD, filling_mode=0)) == ORDER_FILLING_RETURN
    assert pick_filling(spec_with(EURUSD, filling_mode=3)) == ORDER_FILLING_FOK


def test_trading_hours():
    cfg = RiskConfig(trading_hours_utc=(7, 20))
    assert in_trading_hours(cfg, datetime(2026, 1, 1, 12, tzinfo=timezone.utc))
    assert not in_trading_hours(cfg, datetime(2026, 1, 1, 22, tzinfo=timezone.utc))
    wrap = RiskConfig(trading_hours_utc=(22, 4))
    assert in_trading_hours(wrap, datetime(2026, 1, 1, 23, tzinfo=timezone.utc))
    assert in_trading_hours(wrap, datetime(2026, 1, 1, 3, tzinfo=timezone.utc))
    assert not in_trading_hours(wrap, datetime(2026, 1, 1, 12, tzinfo=timezone.utc))
    assert in_trading_hours(RiskConfig(), None)


# ---- pérdida diaria ---------------------------------------------------------------------------


def test_daily_loss_hit_uses_day_start_balance():
    acct = AccountSnapshot(login=1, trade_mode=0, balance=9_750, equity=9_700, currency="USD")
    assert not daily_loss_hit(CFG, acct, RiskState(realized_today=-250, floating_pnl=-40))
    assert daily_loss_hit(CFG, acct, RiskState(realized_today=-250, floating_pnl=-60))


def test_daily_loss_boundary_exactly_at_limit_and_float_noise():
    acct = AccountSnapshot(login=1, trade_mode=0, balance=9_700, equity=9_700, currency="USD")
    assert daily_loss_hit(CFG, acct, RiskState(realized_today=-300))
    assert not daily_loss_hit(CFG, acct, RiskState(realized_today=-299.99))
    noisy = AccountSnapshot(login=1, trade_mode=0, balance=9_899.9, equity=9_700, currency="USD")
    assert daily_loss_hit(CFG, noisy, RiskState(realized_today=-100.1, floating_pnl=-199.9))


def test_deposit_today_does_not_widen_daily_limit():
    acct = AccountSnapshot(login=1, trade_mode=0, balance=15_000 - 200, equity=14_800, currency="USD")
    st = RiskState(realized_today=-200, balance_ops_today=5_000)
    assert day_start_balance(acct, st) == 10_000
    assert not daily_loss_hit(CFG, acct, st)
    worse = AccountSnapshot(login=1, trade_mode=0, balance=15_000 - 300, equity=14_700, currency="USD")
    assert daily_loss_hit(CFG, worse, RiskState(realized_today=-300, balance_ops_today=5_000))


# ---- evaluate_order ---------------------------------------------------------------------------


def test_evaluate_ok_sizes_volume_and_reports_prices():
    ev = _ev(OrderIntent("EURUSD", "buy", sl=1.0981, tp=1.1041))
    assert ev.ok, ev.reasons
    assert ev.volume == 0.5 and ev.risk_pct == pytest.approx(1.0)
    assert ev.entry_price == 1.1001 and ev.close_ref_price == 1.1000 and ev.sl_points == 200
    assert ev.sl == 1.0981 and ev.tp == 1.1041 and not ev.warnings


def test_evaluate_rejects_live_without_allow():
    ev = _ev(OrderIntent("EURUSD", "buy", sl=1.0981), acct=REAL)
    assert not ev.ok and any("REAL" in r for r in ev.reasons)
    ev2 = evaluate_order(RiskConfig(allow_live=True), REAL, EURUSD, STATE, OrderIntent("EURUSD", "buy", sl=1.0981), 1.1000, 1.1001)
    assert ev2.ok


def test_evaluate_requires_sl_and_correct_side_against_close_price():
    assert not _ev(OrderIntent("EURUSD", "buy", sl=0)).ok
    assert any("por debajo del bid" in r for r in _ev(OrderIntent("EURUSD", "buy", sl=1.1050)).reasons)
    assert any("por encima del ask" in r for r in _ev(OrderIntent("EURUSD", "sell", sl=1.0950)).reasons)
    assert any("TP" in r and "por encima" in r for r in _ev(OrderIntent("EURUSD", "buy", sl=1.0981, tp=1.0990)).reasons)
    assert any("TP" in r and "por debajo" in r for r in _ev(OrderIntent("EURUSD", "sell", sl=1.1021, tp=1.1050)).reasons)
    # SL de una compra entre bid y ask: por debajo del ask pero no del bid -> inválido
    assert any("por debajo del bid" in r for r in _ev(OrderIntent("EURUSD", "buy", sl=1.10005)).reasons)


def test_evaluate_rejects_nan_and_inf_inputs():
    nan = float("nan")
    assert any("stop loss obligatorio" in r for r in _ev(OrderIntent("EURUSD", "buy", sl=nan)).reasons)
    assert any("take profit inválido" in r for r in _ev(OrderIntent("EURUSD", "buy", sl=1.0981, tp=nan)).reasons)
    assert any("volumen inválido" in r for r in _ev(OrderIntent("EURUSD", "buy", sl=1.0981, volume=float("inf"))).reasons)
    assert any("risk_pct inválido" in r for r in _ev(OrderIntent("EURUSD", "buy", sl=1.0981, risk_pct=nan)).reasons)
    assert not _ev(OrderIntent("EURUSD", "buy", sl=float("inf"))).ok


def test_evaluate_respects_stops_level_from_close_price_and_min_sl():
    ev = _ev(OrderIntent("XAUUSD", "buy", sl=2000.20), spec=XAUUSD, bid=2000.50, ask=2000.80)
    assert any("demasiado cerca" in r for r in ev.reasons)
    ok = _ev(OrderIntent("XAUUSD", "buy", sl=1999.90), spec=XAUUSD, bid=2000.50, ask=2000.80)
    assert ok.ok, ok.reasons
    tp_close = _ev(OrderIntent("XAUUSD", "buy", sl=1999.90, tp=2000.80), spec=XAUUSD, bid=2000.50, ask=2000.80)
    assert any("TP demasiado cerca" in r for r in tp_close.reasons)
    cfg = RiskConfig(symbols=("EURUSD",), min_sl_points=300)
    assert any("mínimo 300" in r for r in _ev(OrderIntent("EURUSD", "buy", sl=1.0981), cfg=cfg).reasons)


def test_evaluate_rejects_symbol_outside_allowlist_and_broker_mode():
    assert any("lista blanca" in r for r in _ev(OrderIntent("GBPUSD", "buy", sl=1.0981), spec=spec_with(EURUSD, name="GBPUSD")).reasons)
    closeonly = spec_with(EURUSD, trade_mode=SYMBOL_TRADE_MODE_CLOSEONLY)
    assert any("no permite abrir" in r for r in _ev(OrderIntent("EURUSD", "buy", sl=1.0981), spec=closeonly).reasons)
    longonly = spec_with(EURUSD, trade_mode=SYMBOL_TRADE_MODE_LONGONLY)
    assert any("long only" in r for r in _ev(OrderIntent("EURUSD", "sell", sl=1.1021), spec=longonly).reasons)


def test_global_blockers():
    base = OrderIntent("EURUSD", "buy", sl=1.0981)
    assert any("kill switch" in r for r in _ev(base, state=RiskState(halted=True)).reasons)
    assert any("AutoTrading" in r for r in _ev(base, state=RiskState(trade_allowed=False)).reasons)
    assert any("API externa de Python" in r for r in _ev(base, state=RiskState(python_api_disabled=True)).reasons)
    assert any("trade_allowed=false" in r for r in _ev(base, state=RiskState(account_trade_allowed=False)).reasons)
    assert any("trade_expert=false" in r for r in _ev(base, state=RiskState(account_expert_allowed=False)).reasons)
    acct = AccountSnapshot(login=1, trade_mode=0, balance=9_690, equity=9_690, currency="USD")
    assert any("pérdida diaria" in r for r in _ev(base, acct=acct, state=RiskState(realized_today=-310)).reasons)
    assert any("posiciones abiertas" in r for r in _ev(base, state=RiskState(open_positions=3)).reasons)
    cfg = RiskConfig(symbols=("EURUSD",), trading_hours_utc=(7, 20))
    late = RiskState(now_utc=datetime(2026, 1, 1, 22, 30, tzinfo=timezone.utc))
    assert any("fuera del horario" in r for r in _ev(base, state=late, cfg=cfg).reasons)
    assert trading_blockers(CFG, DEMO, RiskState()) == []


def test_evaluate_require_tp():
    cfg = RiskConfig(symbols=("EURUSD",), require_tp=True, max_volume_per_order=1.0, max_total_volume=2.0)
    assert any("take profit obligatorio" in r for r in _ev(OrderIntent("EURUSD", "buy", sl=1.0981), cfg=cfg).reasons)
    assert _ev(OrderIntent("EURUSD", "buy", sl=1.0981, tp=1.1041), cfg=cfg).ok


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
    ev2 = _ev(OrderIntent("EURUSD", "buy", sl=1.0981), spec=spec_with(EURUSD, spread=80))
    assert any("un tercio" in w for w in ev2.warnings)


def test_evaluate_rejects_bad_quotes():
    assert any("cotización inválida" in r for r in _ev(OrderIntent("EURUSD", "buy", sl=1.0981), bid=0.0, ask=0.0).reasons)
    assert any("cotización inválida" in r for r in _ev(OrderIntent("EURUSD", "buy", sl=1.0981), bid=1.1002, ask=1.1001).reasons)
    assert any("cotización inválida" in r for r in _ev(OrderIntent("EURUSD", "buy", sl=1.0981), bid=float("nan"), ask=1.1001).reasons)


def test_evaluate_index_with_tick_size_and_step():
    ev = _ev(OrderIntent("US500", "buy", sl=4990.30), spec=US500, bid=5000.00, ask=5000.50)
    # entrada 5000.50, SL 4990.30 -> 10.2 de distancia = 40.8 ticks * 0.25 = 10.2 USD por lote; riesgo 100 -> 9.8 lotes -> cap 1.0
    assert ev.ok, ev.reasons
    assert ev.volume == 1.0 and ev.loss_per_lot == pytest.approx(10.2)
