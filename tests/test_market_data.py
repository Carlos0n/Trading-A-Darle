"""Reloj del servidor, indicadores y velas."""

from datetime import datetime, timezone

import pytest

from mt5_mcp.broker import BrokerError
from mt5_mcp.indicators import atr, ema, ema_series, rsi, sma
from mt5_mcp.timeutil import CACHE_TTL, ServerClock, candidate_offset

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc).timestamp()
H = 3600


# ---- reloj del servidor -------------------------------------------------------------------------


def test_candidate_offset_from_fresh_ticks():
    assert candidate_offset([int(NOW)], NOW) == 0
    assert candidate_offset([int(NOW + 3 * H)], NOW) == 3 * H
    assert candidate_offset([int(NOW + 3 * H - 20)], NOW) == 3 * H           # tick de hace 20 s
    assert candidate_offset([int(NOW - 5 * H)], NOW) == -5 * H               # servidor por detrás de UTC
    assert candidate_offset([int(NOW + 5.5 * H)], NOW) == int(5.5 * H)       # husos de media hora
    assert candidate_offset([int(NOW + 3 * H - 900), int(NOW + 3 * H - 5)], NOW) == 3 * H  # manda el más fresco
    assert candidate_offset([int(NOW + 3 * H + 10)], NOW) == 3 * H           # reloj del PC algo atrasado


def test_candidate_offset_rejects_stale_or_absurd():
    assert candidate_offset([], NOW) is None
    assert candidate_offset([0], NOW) is None
    assert candidate_offset([int(NOW + 3 * H - 300)], NOW) is None           # 5 minutos sin ticks: no concluyente
    assert candidate_offset([int(NOW + 3 * H - 48 * H)], NOW) is None        # fin de semana: fuera de rango
    assert candidate_offset([int(NOW + 20 * H)], NOW) is None


def test_server_clock_keeps_last_good_offset_while_market_is_quiet():
    clock = ServerClock()
    assert clock.update([], NOW) is None and not clock.known and clock.age(123, NOW) is None
    assert clock.update([int(NOW + 3 * H)], NOW) == 3 * H and clock.known
    later = NOW + 2 * H  # dos horas sin ticks: un candidato "UTC+1" sería falso
    assert clock.update([int(NOW + 3 * H)], later) == 3 * H
    assert clock.age(int(NOW + 3 * H), later) == pytest.approx(2 * H)
    assert clock.server_now(later) == later + 3 * H


def test_server_clock_follows_dst_changes():
    clock = ServerClock()
    clock.update([int(NOW + 2 * H)], NOW)
    # el servidor adelanta una hora: un tick "del futuro" obliga a subir el desfase de inmediato
    assert clock.update([int(NOW + 60 + 3 * H)], NOW + 60) == 3 * H
    # el servidor atrasa una hora: se conserva el anterior hasta que caduca sin confirmación
    assert clock.update([int(NOW + 120 + 2 * H)], NOW + 120) == 3 * H
    expired = NOW + 120 + CACHE_TTL + 1
    assert clock.update([int(expired + 2 * H)], expired) == 2 * H


def test_server_clock_forgets_after_long_silence():
    clock = ServerClock()
    clock.update([int(NOW + 3 * H)], NOW)
    weekend = NOW + 50 * H
    assert clock.update([int(NOW + 3 * H)], weekend) is None and not clock.known
    assert clock.update([int(weekend + 3 * H)], weekend) == 3 * H


# ---- indicadores --------------------------------------------------------------------------------


def test_sma_and_ema():
    assert sma([1, 2, 3, 4], 2) == 3.5 and sma([1], 2) is None
    assert ema([1, 2, 3, 4, 5], 3) == pytest.approx(4.0)  # semilla 2; luego 3 y 4
    assert ema_series([1, 2, 3, 4, 5], 3) == pytest.approx([2.0, 3.0, 4.0])
    assert ema([7.0] * 60, 20) == pytest.approx(7.0)
    assert ema([1, 2], 3) is None and ema([], 3) is None and ema([1, 2, 3], 0) is None


def test_rsi():
    assert rsi(list(range(1, 30)), 14) == pytest.approx(100.0)
    assert rsi(list(range(30, 1, -1)), 14) == pytest.approx(0.0)
    assert rsi([5.0] * 30, 14) == 50.0
    assert rsi([10, 11] * 8, 14) == pytest.approx(50.0, abs=4)
    assert rsi([1.0] * 14, 14) is None
    up = rsi([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 13.5], 14)
    assert 90 < up < 100


def test_atr():
    highs = [10 + i * 0.0 + 1 for i in range(30)]
    lows = [10 - 1 for _ in range(30)]
    closes = [10.0] * 30
    assert atr(highs, lows, closes, 14) == pytest.approx(2.0)
    assert atr(highs[:10], lows[:10], closes[:10], 14) is None
    assert atr(highs, lows[:-1], closes, 14) is None
    gap = atr([10, 20, 20, 20], [9, 19, 19, 19], [9.5, 19.5, 19.5, 19.5], 2)  # un hueco cuenta en el rango verdadero
    assert gap == pytest.approx(((20 - 9.5) + 1) / 2 * 0.5 + 0.5)


# ---- velas --------------------------------------------------------------------------------------


def test_bars_indicators_use_closed_candles_only(engine, fake):
    closes = [1.0500 + i * 0.0002 for i in range(280)]  # tendencia alcista
    closes[-1] = 0.9000  # la vela en formación no debe contaminar nada
    fake.set_rates("EURUSD", fake.TIMEFRAME_M15, closes)
    b = engine.bars("eurusd", "m15", 10)
    ind = b["indicators"]
    assert b["symbol"] == "EURUSD" and b["timeframe"] == "M15" and b["closed_bars_used"] == 260
    assert len(b["bars"]) == 10 and b["bars"][-1][4] == pytest.approx(closes[-2]) and b["forming_bar"]["close"] == 0.9
    assert ind["trend"] == "up" and ind["last_close"] == pytest.approx(closes[-2], abs=1e-6)
    assert ind["ema20"] > ind["ema50"] and ind["rsi14"] == 100.0
    assert ind["atr14"] == pytest.approx(0.0012, abs=1e-6) and ind["atr14_points"] == pytest.approx(120, abs=0.5)
    assert ind["highest_high_20"] == pytest.approx(closes[-2] + 0.0005, abs=1e-6)
    assert ind["close_minus_ema20_in_atr"] > 0
    assert b["quote"]["bid"] == 1.1 and b["quote"]["stale"] is False
    assert b["bars"][0][0] < b["bars"][-1][0] and len(b["bars"][0][0]) == 16  # "AAAA-MM-DD HH:MM", orden ascendente


def test_bars_downtrend_flat_and_short_history(engine, fake):
    fake.set_rates("EURUSD", fake.TIMEFRAME_H1, [1.2 - i * 0.0002 for i in range(280)], step=3600)
    assert engine.bars("EURUSD", "H1")["indicators"]["trend"] == "down"
    fake.set_rates("EURUSD", fake.TIMEFRAME_M5, [1.1, 1.1001, 1.1002, 1.1001, 1.1], step=300)
    short = engine.bars("EURUSD", "M5", 50)
    assert short["indicators"]["ema50"] is None and short["indicators"]["trend"] == "unknown" and len(short["bars"]) == 4
    assert short["indicators"]["rsi14"] is None and short["indicators"]["atr14"] is None


def test_bars_errors(engine, fake):
    with pytest.raises(BrokerError, match="timeframe inválido"):
        engine.bars("EURUSD", "W1")
    with pytest.raises(BrokerError, match="no hay velas suficientes"):
        engine.bars("EURUSD", "M15")
    fake.fail_rates = True
    with pytest.raises(BrokerError, match="copy_rates_from_pos"):
        engine.bars("EURUSD", "M15")
    with pytest.raises(BrokerError, match="no existe"):
        engine.bars("NOPE", "M15")
