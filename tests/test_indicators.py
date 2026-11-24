"""Indicadores (EMA, RSI y ATR): casos hechos a mano y una serie con valores de referencia.

Las referencias de CLOSES no salen de indicators.py: están calculadas aparte con fracciones exactas y con otra
formulación (suma ponderada explícita en lugar de la recurrencia).
"""

import pytest

from mt5_mcp.indicators import atr, ema, ema_series, rsi, sma

# Serie de ejemplo clásica del RSI de 14 periodos: 33 cierres con subidas, bajadas y un cierre repetido.
CLOSES = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03,
          46.41, 46.22, 45.64, 46.21, 46.25, 45.71, 46.45, 45.78, 45.35, 44.03, 44.18, 44.22, 44.57, 43.42, 42.66, 43.13]
# RSI de 14 con los primeros 15, 16, ..., 33 cierres.
REF_RSI14 = [70.4641, 66.2496, 66.4809, 69.3469, 66.2947, 57.9150, 62.8807, 63.2088, 56.0116, 62.3399, 54.6710, 50.3868, 40.0194,
             41.4926, 41.9024, 45.4995, 37.3228, 33.0905, 37.7888]
REF_EMA10 = 44.1193  # EMA de 10 y de 20 con los 33 cierres
REF_EMA20 = 44.6354


def test_sma_and_ema():
    assert sma([1, 2, 3, 4], 2) == 3.5 and sma([1], 2) is None
    assert ema([1, 2, 3, 4, 5], 3) == pytest.approx(4.0)  # semilla 2; luego 3 y 4
    assert ema_series([1, 2, 3, 4, 5], 3) == pytest.approx([2.0, 3.0, 4.0])
    assert ema([7.0] * 60, 20) == pytest.approx(7.0)
    assert ema([1, 2], 3) is None and ema([], 3) is None and ema([1, 2, 3], 0) is None


def test_ema_is_not_a_simple_average_of_the_last_values():
    assert ema([1, 2, 3, 10], 3) == pytest.approx(6.0)  # semilla 2; luego 10 x 0.5 + 2 x 0.5. La media de los 3 últimos sería 5
    assert ema_series([2, 4, 6, 8, 20], 2) == pytest.approx([3.0, 5.0, 7.0, 47 / 3])  # k = 2/3
    line = [float(i) for i in range(100)]  # en una recta, la EMA de n periodos va (n - 1) / 2 valores por detrás del último
    assert ema(line, 20) == pytest.approx(99 - 9.5) and ema(line, 50) == pytest.approx(99 - 24.5)
    assert ema(CLOSES, 10) == pytest.approx(REF_EMA10, abs=1e-4) and ema(CLOSES, 20) == pytest.approx(REF_EMA20, abs=1e-4)


def test_rsi():
    assert rsi(list(range(1, 30)), 14) == pytest.approx(100.0)
    assert rsi(list(range(30, 1, -1)), 14) == pytest.approx(0.0)
    assert rsi([5.0] * 30, 14) == 50.0
    assert rsi([10, 11] * 8, 14) == pytest.approx(50.0, abs=4)
    assert rsi([1.0] * 14, 14) is None
    up = rsi([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 13.5], 14)
    assert 90 < up < 100


def test_rsi_wilder_smoothing_by_hand():
    # Periodo 3 y cambios +1, -1, +2: medias iniciales de ganancia 3/3 y de pérdida 1/3, RS = 3.
    assert rsi([10, 11, 10, 12], 3) == pytest.approx(75.0)
    # Un cuarto cambio de -1 las deja en (1 x 2 + 0) / 3 = 2/3 y (1/3 x 2 + 1) / 3 = 5/9: RS = 1.2.
    assert rsi([10, 11, 10, 12, 11], 3) == pytest.approx(100 - 100 / 2.2)
    # Un cierre repetido también cuenta: baja las dos medias a 2/3 y 2/9, y el -1 siguiente las deja en 4/9 y 13/27.
    assert rsi([10, 11, 10, 12, 12, 11], 3) == pytest.approx(48.0)


def test_rsi_reference_series():
    # Cada valor arrastra todos los cierres anteriores: no vale calcularlo con los 15 primeros ni con los 15 últimos.
    got = [rsi(CLOSES[:n], 14) for n in range(15, len(CLOSES) + 1)]
    assert got == pytest.approx(REF_RSI14, abs=1e-3)


def test_atr():
    highs = [10 + i * 0.0 + 1 for i in range(30)]
    lows = [10 - 1 for _ in range(30)]
    closes = [10.0] * 30
    assert atr(highs, lows, closes, 14) == pytest.approx(2.0)
    assert atr(highs[:10], lows[:10], closes[:10], 14) is None
    assert atr(highs, lows[:-1], closes, 14) is None
    gap = atr([10, 20, 20, 20], [9, 19, 19, 19], [9.5, 19.5, 19.5, 19.5], 2)  # un hueco cuenta en el rango verdadero
    assert gap == pytest.approx(((20 - 9.5) + 1) / 2 * 0.5 + 0.5)


def test_atr_gap_down_and_wilder_smoothing_by_hand():
    # Hueco bajista: la segunda vela va de 4 a 5, pero el cierre anterior fue 9.5. Su rango verdadero es 9.5 - 4.
    assert atr([10, 5, 5], [9, 4, 4], [9.5, 4.5, 4.5], 2) == pytest.approx((5.5 + 1) / 2)
    # Rangos 3, 1, 2 y 6 con periodo 3: la media inicial es 2 y la cuarta vela la deja en (2 x 2 + 6) / 3.
    highs, lows, closes = [10, 11.5, 10.5, 11, 13], [10, 8.5, 9.5, 9, 7], [10.0] * 5
    assert atr(highs[:3], lows[:3], closes[:3], 3) is None  # con 3 velas solo hay 2 rangos verdaderos
    assert atr(highs[:4], lows[:4], closes[:4], 3) == pytest.approx(2.0)
    assert atr(highs, lows, closes, 3) == pytest.approx(10 / 3)


def test_period_limits_and_defaults():
    assert sma([1, 2], 2) == 1.5 and ema([1, 2, 3], 3) == 2.0  # con justo n valores, la EMA de n es su media
    assert sma([1, 2], 0) is None and rsi([1, 2, 3], 0) is None and atr([2, 2], [1, 1], [1, 1], 0) is None  # sin dividir por cero
    highs, lows = [c + 0.3 for c in CLOSES], [c - 0.2 for c in CLOSES]
    assert rsi(CLOSES) == rsi(CLOSES, 14) and atr(highs, lows, CLOSES) == atr(highs, lows, CLOSES, 14)  # por defecto, 14 periodos
