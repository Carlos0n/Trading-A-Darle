"""Reloj del servidor y velas con sus indicadores (las funciones de indicators.py se prueban en test_indicators.py)."""

from dataclasses import replace
from datetime import datetime, timezone

import pytest
from conftest import World
from test_indicators import CLOSES, REF_EMA20, REF_RSI14

from mt5_mcp.broker import TF_SECONDS, TIMEFRAMES, BrokerError, MT5Broker
from mt5_mcp.timeutil import CACHE_TTL, MAX_BEHIND, ServerClock, candidate_offset

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc).timestamp()
H = 3600


# ---- reloj del servidor: reglas puras -------------------------------------------------------------


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
    # Lo que no puede distinguir por sí sola: un tick parado hace 30 min y 25 s parece uno fresco de otro huso.
    # Por eso ServerClock solo le pasa ticks que ha visto moverse.
    assert candidate_offset([int(NOW + 3 * H - 1825)], NOW) == int(2.5 * H)


def known_clock(offset: int, **frozen: int) -> ServerClock:
    """Reloj con el desfase ya confirmado en NOW + 2: un tick de E se movió entre dos lecturas cercanas."""
    clock = ServerClock()
    assert clock.update({"E": int(NOW + offset), **frozen}, NOW) is None
    assert clock.update({"E": int(NOW + 2 + offset), **frozen}, NOW + 2) == offset
    return clock


def test_server_clock_needs_a_tick_that_moves():
    clock = ServerClock()
    assert clock.update({}, NOW) is None and not clock.known
    assert clock.age(123, NOW) is None and clock.server_now(NOW) is None      # desconocido no es UTC
    assert clock.update({"E": int(NOW + 3 * H)}, NOW) is None                 # una sola lectura no prueba nada
    assert clock.update({"E": int(NOW + 3 * H)}, NOW + 1) is None             # el tick no se ha movido
    assert not clock.is_live(NOW + 1)
    assert clock.update({"E": int(NOW + 2 + 3 * H)}, NOW + 2) == 3 * H        # se movió entre dos lecturas cercanas
    assert clock.known and clock.is_live(NOW + 2) and clock.server_now(NOW + 2) == NOW + 2 + 3 * H
    assert clock.age(int(NOW + 3 * H), NOW + 2) == pytest.approx(2.0)


@pytest.mark.parametrize("k", [1, 2, 4, 13, 27])
def test_server_clock_never_accepts_a_frozen_tick_on_the_half_hour_grid(k):
    last = int(NOW + 2 * H)                 # último tick antes del cierre, servidor en UTC+2
    t = NOW + k * 1800 + 25                 # k x 30 min y 25 s después
    assert candidate_offset([last], t) == 2 * H - k * 1800  # una lectura suelta lo tomaría por fresco
    ticks = {"E": last, "G": last - 40}
    cold = ServerClock()                    # proceso recién arrancado: sin lectura anterior
    for i in range(13):                     # sondeo de arranque, cada medio segundo
        assert cold.update(ticks, t + i * 0.5) is None
    for i in range(1, 200):                 # y con lecturas anteriores, cada 30 s, cruzando más medias horas
        assert cold.update(ticks, t + 6 + i * 30) is None
    assert not cold.known and not cold.is_live(t + 6 + 199 * 30)


def test_server_clock_ignores_a_change_seen_against_an_old_reading():
    # PC suspendido con el mercado abierto y reanudado tras el cierre: el tick cambió, pero respecto a una lectura
    # de hace horas, y el de ahora está parado a 13 x 30 min y 25 s.
    clock = ServerClock()
    clock.update({"E": int(NOW + 2 * H)}, NOW)
    later = NOW + 5 * H
    assert clock.update({"E": int(later - 13 * 1800 - 25 + 2 * H)}, later) is None
    # lo mismo con un desfase que estaba confirmado y ya caducó
    clock = known_clock(2 * H)
    later = NOW + CACHE_TTL + 2 * H
    assert clock.update({"E": int(later - 1825 + 2 * H)}, later) is None and not clock.known


def test_server_clock_keeps_last_good_offset_while_market_is_quiet():
    clock = known_clock(3 * H)
    last = int(NOW + 2 + 3 * H)
    later = NOW + 2 * H  # dos horas sin ticks: un candidato "UTC+1" sería falso
    assert clock.update({"E": last}, later) == 3 * H
    assert clock.age(last, later) == pytest.approx(2 * H - 2)
    assert clock.server_now(later) == later + 3 * H
    for i in range(1, 400):  # sondeo cada 30 s hasta pasar las 12 h: o el desfase bueno, o ninguno
        assert clock.update({"E": last}, later + i * 30 * 4) in (3 * H, None)
    assert not clock.known  # caducó sin confirmación


def test_server_clock_follows_winter_time_with_a_frozen_symbol_next_to_a_live_one():
    stuck = int(NOW + 3 * H - 5000)  # X dejó de cotizar hace rato
    clock = known_clock(3 * H, X=stuck)
    t = NOW + 60  # el servidor atrasa una hora: el primer tick que se mueve lo corrige
    assert clock.update({"E": int(t + 2 * H), "X": stuck}, t) == 2 * H
    assert clock.age(int(t + 2 * H), t) == 0
    # en una lectura aislada (la anterior queda lejos) no se puede saber: se conserva hasta la siguiente cercana
    clock = known_clock(3 * H, X=stuck)
    t = NOW + 600
    assert clock.update({"E": int(t + 2 * H), "X": stuck}, t) == 3 * H
    assert clock.update({"E": int(t + 3 + 2 * H), "X": stuck}, t + 3) == 2 * H


def test_server_clock_follows_summer_time():
    stuck = int(NOW + 2 * H - 5000)
    clock = known_clock(2 * H, X=stuck)
    t = NOW + 60  # el servidor adelanta una hora
    assert clock.update({"E": int(t + 3 * H), "X": stuck}, t) == 3 * H
    # lectura aislada: un tick "del futuro" contradice el desfase guardado; se descarta y se vuelve a medir
    clock = known_clock(2 * H, X=stuck)
    t = NOW + 600
    assert clock.update({"E": int(t + 3 * H), "X": stuck}, t) is None
    assert clock.update({"E": int(t + 1 + 3 * H), "X": stuck}, t + 1) == 3 * H


def test_server_clock_uses_only_the_symbol_that_moves():
    stuck = int(NOW + 3 * H - 3600 - 10)  # parado hace 1 h y 10 s: solo, parecería un tick fresco de UTC+2
    clock = ServerClock()
    assert clock.update({"X": stuck, "E": int(NOW + 3 * H)}, NOW) is None
    assert clock.update({"X": stuck, "E": int(NOW + 1 + 3 * H)}, NOW + 1) == 3 * H
    assert clock.age(stuck, NOW + 1) == pytest.approx(3611)
    alone = ServerClock()
    for i in range(10):
        assert alone.update({"X": stuck}, NOW + i) is None


def test_server_clock_forgets_after_long_silence_and_recognises_the_reopening():
    clock = known_clock(3 * H)
    last = int(NOW + 2 + 3 * H)
    weekend = NOW + 50 * H
    assert clock.update({"E": last}, weekend) is None and not clock.known
    # reabre: el primer tick que se mueve podría ser uno parado que el terminal acaba de sincronizar; el segundo confirma
    assert clock.update({"E": int(weekend + 5 + 3 * H)}, weekend + 5) is None and clock.pending == 3 * H
    assert clock.update({"E": int(weekend + 6 + 3 * H)}, weekend + 6) == 3 * H and clock.pending is None


def test_server_clock_is_not_fooled_by_a_single_change_to_another_stale_tick():
    # PC suspendido el viernes a las 15:00 UTC con el mercado abierto y reanudado el sábado: al reconectar, el
    # terminal cambia su último tick (15:00) por el último del servidor (cierre, 21:59:58 UTC). El tick "se mueve"
    # entre dos lecturas cercanas, pero lleva parado 5 h y 32 s: no es un tick nuevo.
    friday_15 = NOW                       # servidor en UTC+2
    close = int(friday_15 + 7 * H - 2 + 2 * H)
    wake = friday_15 + 12 * H + 30        # sábado 03:00:30 UTC
    cold = ServerClock()                  # sin desfase guardado (o caducado)
    assert cold.update({"E": int(friday_15 + 2 * H)}, wake) is None
    assert cold.update({"E": close}, wake + 0.5) is None and cold.pending == -3 * H  # lo que diría ese tick: UTC-3
    for i in range(2, 400):
        assert cold.update({"E": close}, wake + i * 0.5) is None
    # con un desfase vigente (suspendido menos de 12 h) tampoco: se conserva el bueno
    clock = known_clock(2 * H)
    wake = NOW + 9 * H + 30
    close = int(wake - 2 * H - 25 + 2 * H)  # parado hace 2 h y 25 s
    assert clock.update({"E": int(NOW + 2 + 2 * H)}, wake) == 2 * H
    for i in range(1, 50):
        assert clock.update({"E": close}, wake + i * 3) == 2 * H
    assert clock.age(close, wake + 150) == pytest.approx(2 * H + 25 + 150)


def test_server_clock_pending_offset_does_not_survive_a_gap_between_readings():
    clock = ServerClock()
    assert clock.update({"E": int(NOW + 2 * H - 9000)}, NOW) is None
    stale = int(NOW + 2 * H - 3600 - 20)  # cambia una vez a otro tick parado (1 h y 20 s): propone UTC+1
    assert clock.update({"E": stale}, NOW + 1) is None and clock.pending == H
    later = NOW + 2 * H  # lectura aislada dos horas después: lo pendiente se olvida
    assert clock.update({"E": stale}, later) is None and clock.pending is None
    again = int(later + 1 + 2 * H - 3600 - 20)  # otro cambio aislado que propondría lo mismo no cuenta como confirmación
    assert clock.update({"E": again}, later + 1) is None and not clock.known


def test_server_clock_tick_from_the_future_or_confirming_the_current_offset_needs_no_second_tick():
    clock = known_clock(2 * H)
    last = int(NOW + 2 + 2 * H)
    for t in range(60, 600, 60):  # el símbolo deja de cotizar 10 minutos (lecturas cercanas)
        assert clock.update({"E": last}, NOW + t) == 2 * H
    t = NOW + 600  # vuelve a cotizar con la misma hora: confirma el desfase vigente
    assert clock.update({"E": int(t + 2 * H)}, t) == 2 * H and clock.confirmed_at == t and clock.pending is None
    quiet = int(t + 2 * H)
    for dt in range(60, 600, 60):
        clock.update({"E": quiet}, t + dt)
    t += 600  # y ahora con el reloj del servidor adelantado: un tick "del futuro" no puede ser uno viejo
    assert clock.update({"E": int(t + 3 * H)}, t) == 3 * H


def test_server_clock_slow_symbol_is_confirmed_by_its_second_tick():
    clock = ServerClock()
    old = int(NOW + 3 * H - 400)  # último tick hace casi 7 minutos: el símbolo cotiza poco
    assert clock.update({"S": old}, NOW) is None
    assert clock.update({"S": int(NOW + 30 + 3 * H)}, NOW + 30) is None and clock.pending == 3 * H
    for t in range(60, 300, 30):  # sigue a la espera mientras las lecturas sean cercanas
        assert clock.update({"S": int(NOW + 30 + 3 * H)}, NOW + t) is None
    assert clock.update({"S": int(NOW + 300 + 3 * H)}, NOW + 300) == 3 * H


@pytest.mark.parametrize("skew,known", [(-20, True), (-45, False), (100, True), (150, False)])
def test_server_clock_tolerates_only_a_small_pc_clock_skew(skew, known):
    clock = ServerClock()  # skew: segundos que el reloj del PC adelanta (+) o atrasa (-) respecto a la hora real
    for i in range(6):
        real = NOW + i
        clock.update({"E": int(real + 3 * H)}, real + skew)
    assert clock.known is known and clock.is_live(NOW + 5 + skew)
    assert clock.offset == (3 * H if known else None)


def test_readings_must_be_close_to_count_as_a_move():
    clock = ServerClock()
    clock.update({"E": int(NOW + 3 * H)}, NOW)
    late = NOW + MAX_BEHIND + 1
    assert clock.update({"E": int(late + 3 * H)}, late) is None       # la lectura anterior queda demasiado lejos
    assert clock.update({"E": int(late + 2 + 3 * H)}, late + 2) == 3 * H


# ---- reloj del servidor: en el engine ---------------------------------------------------------------


def test_cold_start_recognises_an_open_market(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, offset=2 * H)
    q = w.engine.quote("EURUSD")
    assert q["stale"] is False and q["age_seconds"] == 0
    assert w.slept == [0.5, 0.5]  # esperó solo hasta ver moverse un tick
    st = w.engine.auto_status()
    assert st["server_clock_known"] and st["server_utc_offset_hours"] == 2.0 and st["can_open"] and st["blockers"] == []
    assert st["cooldown_remaining_minutes"] == 0.0 and st["day_window"].endswith("UTC")
    assert len(w.slept) == 2  # con el reloj ya conocido no se vuelve a esperar


@pytest.mark.parametrize("minutes", [30, 60, 120, 390, 810])
def test_fresh_process_never_takes_a_frozen_quote_for_fresh(tmp_path, monkeypatch, minutes):
    w = World(tmp_path, monkeypatch, offset=2 * H)
    w.fake.freeze_ticks()             # el mercado cierra
    w.t += minutes * 60 + 25          # el ciclo corre N x 30 min después, 25 s tras el cierre de vela
    eng = w.engine                    # sin ninguna llamada previa: como cada proceso que lanza el bucle
    q = eng.quote("EURUSD")
    assert q["stale"] is True and q["age_seconds"] is None
    assert sum(w.slept) == 6.0        # esperó lo configurado, ni más ni menos
    st = eng.auto_status()
    assert st["server_clock_known"] is False and st["server_utc_offset_hours"] is None
    assert st["can_open"] is False and any("hora del servidor desconocida" in b for b in st["blockers"])
    assert any("no llega ninguna cotización nueva" in b for b in st["blockers"])
    assert st["cooldown_remaining_minutes"] is None and "SIN VERIFICAR" in st["day_window"]
    r = w.buy("EURUSD")
    assert not r["sent"] and not w.fake.sent and not w.fake.checked
    for _ in range(40):               # con lecturas anteriores recientes tampoco, y sin volver a esperar
        w.t += 45
        assert eng.quote("EURUSD")["stale"] is True and not eng.clock.known
    assert sum(w.slept) == 6.0


@pytest.mark.parametrize("step", [30, 900, 1800])
def test_long_lived_process_never_adopts_a_false_offset_with_the_market_closed(tmp_path, monkeypatch, step):
    w = World(tmp_path, monkeypatch, offset=2 * H)
    assert w.engine.auto_status()["server_utc_offset_hours"] == 2.0
    w.fake.freeze_ticks()
    closed_at = w.t
    w.t += 25
    seen = set()
    for _ in range(int(30 * H / step)):  # 30 horas de mercado cerrado, consultando cada `step` segundos
        w.t += step
        q = w.engine.quote("EURUSD")
        seen.add(w.engine.clock.offset)
        assert q["age_seconds"] in (None, int(w.t - closed_at))  # la antigüedad real, o ninguna
        assert q["stale"] is (q["age_seconds"] is None or q["age_seconds"] > 300)
    assert seen == {2 * H, None}  # el desfase bueno mientras vale y después ninguno; nunca uno falso
    assert w.engine.quote("EURUSD")["stale"] is True


def test_reopening_is_recognised(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, offset=3 * H)
    w.fake.freeze_ticks()
    w.advance(40 * 60)
    assert w.engine.auto_status()["server_clock_known"] is False
    w.advance(1)
    w.fake.touch_ticks()  # el mercado reabre
    before = sum(w.slept)
    st = w.engine.auto_status()
    assert st["server_clock_known"] and st["server_utc_offset_hours"] == 3.0 and st["can_open"]
    assert sum(w.slept) - before <= 1.0  # el primer tick tras el parón pide confirmación: espera solo al siguiente
    fresh = w.new_engine()  # y un proceso nuevo también lo reconoce
    q = fresh.quote("GBPUSD")
    assert q["stale"] is False and fresh.clock.offset == 3 * H


@pytest.mark.parametrize("asleep_hours", [5, 13])  # con el desfase guardado aún vigente, y ya caducado
def test_terminal_resync_after_a_suspended_pc_does_not_set_a_false_clock(tmp_path, monkeypatch, asleep_hours):
    w = World(tmp_path, monkeypatch, offset=2 * H)
    assert w.engine.auto_status()["server_utc_offset_hours"] == 2.0  # viernes, mercado abierto
    w.fake.freeze_ticks()  # el PC se suspende: el terminal se queda con estos ticks
    w.t += asleep_hours * H  # se reanuda con el mercado ya cerrado
    assert w.engine.quote("EURUSD")["stale"] is True  # primera lectura tras reanudar: aún con los ticks de antes
    w.t += 2
    for name in ("EURUSD", "GBPUSD"):  # el terminal reconecta y recibe el último tick del servidor: parado hace 2 h 30 min y 25 s
        w.fake.set_tick(name, 1.1, 1.1001, time=w.fake.server_now() - 5 * 1800 - 25)
    for _ in range(30):  # el panel o la sesión siguen consultando cada pocos segundos
        q = w.engine.quote("EURUSD")
        assert q["stale"] is True and w.engine.clock.offset in (None, 2 * H)  # nunca el falso UTC-0:30
        assert q["age_seconds"] is None or q["age_seconds"] > 2 * H
        w.t += 3
    assert w.engine.risk_status()["can_open_new_positions"] is (asleep_hours == 5)  # con la hora buena aún vigente se puede evaluar


def test_slow_symbol_needs_a_second_tick_and_the_reason_says_so(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, symbols=("EURUSD",))
    w.fake.freeze_ticks("EURUSD", age=400)  # el único símbolo de referencia cotiza poco
    assert w.engine.risk_status()["server_clock_known"] is False
    w.t += 20
    w.fake.set_tick("EURUSD", 1.1, 1.1001, time=w.fake.server_now())  # llega un tick y vuelve el silencio
    rs = w.engine.risk_status()
    assert rs["server_clock_known"] is False and any("hace falta otra para confirmar" in b for b in rs["blockers"])
    waited = sum(w.slept)
    assert w.engine.risk_status()["server_clock_known"] is False and sum(w.slept) == waited  # sin tick nuevo no espera más
    w.t += 90
    w.fake.set_tick("EURUSD", 1.1, 1.1001, time=w.fake.server_now())  # el segundo tick lo confirma
    assert w.engine.risk_status()["server_clock_known"] is True and sum(w.slept) == waited


def test_engine_follows_winter_and_summer_time(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, offset=3 * H)
    w.fake.freeze_ticks("GBPUSD")  # un símbolo parado junto a uno que cotiza
    assert w.engine.auto_status()["server_utc_offset_hours"] == 3.0
    w.t += 30
    w.fake.server_offset = 2 * H  # el servidor atrasa su reloj una hora
    q = w.engine.quote("EURUSD")
    assert q["age_seconds"] == 0 and q["stale"] is False and w.engine.clock.offset == 2 * H
    w.t += 30
    w.fake.server_offset = 3 * H  # y lo adelanta
    assert w.engine.quote("EURUSD")["age_seconds"] == 0 and w.engine.clock.offset == 3 * H
    w.advance(20)
    w.fake.server_offset = 4 * H  # adelanto visto en una llamada aislada: se vuelve a medir en esa misma llamada
    before = sum(w.slept)
    assert w.engine.quote("EURUSD")["age_seconds"] == 0 and w.engine.clock.offset == 4 * H
    assert sum(w.slept) - before <= 1.0


def test_pc_clock_skew_beyond_tolerance_leaves_the_clock_unknown(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, offset=3 * H)
    eng = w.engine
    monkeypatch.setattr(eng, "_now", lambda: datetime.fromtimestamp(w.t - 45, tz=timezone.utc))  # PC 45 s atrasado
    rs = eng.risk_status()
    assert rs["server_clock_known"] is False and rs["can_open_new_positions"] is False
    assert any("hora del servidor desconocida" in b and "reloj de este PC" in b for b in rs["blockers"])
    assert sum(w.slept) <= 1.0  # vio moverse un tick que no encaja: esperar más no lo arregla
    assert eng.quote("EURUSD")["stale"] is True and eng.quote("EURUSD")["age_seconds"] is None
    ok = World(tmp_path, monkeypatch, offset=3 * H)
    monkeypatch.setattr(ok.engine, "_now", lambda: datetime.fromtimestamp(ok.t - 20, tz=timezone.utc))  # dentro de la tolerancia
    assert ok.engine.risk_status()["server_clock_known"] is True


def test_clock_wait_zero_never_sleeps(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, offset=2 * H, clock_wait=0)  # como puede usarlo el panel, que sondea cada pocos segundos
    assert w.engine.auto_status()["server_clock_known"] is False
    w.t += 3
    assert w.engine.auto_status()["server_clock_known"] is True and w.slept == []


def test_clock_wait_is_the_maximum_total_wait(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, clock_wait=1.2)
    w.fake.freeze_ticks()
    assert w.engine.auto_status()["server_clock_known"] is False
    assert w.slept == [0.5, 0.5, pytest.approx(0.2)]


def test_empty_whitelist_uses_market_watch_as_clock_reference(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, offset=3 * H, symbols=(), auto=None)
    calls = []
    orig = w.fake.symbols_get
    monkeypatch.setattr(w.fake, "symbols_get", lambda *a, **k: calls.append(1) or orig(*a, **k))
    rs = w.engine.risk_status()  # sin posiciones ni lista blanca: las referencias salen de Observación de Mercado
    assert rs["server_clock_known"] and rs["server_utc_offset_hours"] == 3.0 and rs["can_open_new_positions"]
    for _ in range(5):
        w.t += 10
        assert w.engine.risk_status()["server_clock_known"]
    assert len(calls) == 1  # la lista de nombres se pide una vez
    assert w.engine.order_send("EURUSD", "buy", sl=1.0981, confirm=True)["sent"]


def test_empty_whitelist_picks_again_the_symbols_that_are_quoting(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, symbols=())
    for i in range(25):
        w.fake.add_symbol(f"SYM{i:02d}")
        w.fake.set_tick(f"SYM{i:02d}", 1.0, 1.1, time=w.fake.server_now() - 5000 - i)
    w.fake.freeze_ticks()  # todo parado: las 20 referencias elegidas no incluyen a SYM24, el de tick más antiguo
    assert w.engine.risk_status()["server_clock_known"] is False
    w.t += 30
    w.fake.touch_ticks("SYM24")  # solo SYM24 empieza a cotizar: mientras no haya hora se vuelve a elegir la lista
    assert w.engine.risk_status()["server_clock_known"] is False  # primera lectura con SYM24: aún sin comparación
    w.t += 5
    assert w.engine.risk_status()["server_clock_known"] is True


def test_whitelist_is_the_only_reference_when_it_is_not_empty(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, symbols=("EURUSD",))
    monkeypatch.setattr(w.fake, "symbols_get", lambda *a, **k: pytest.fail("no debe leer Observación de Mercado"))
    w.fake.freeze_ticks("EURUSD")  # GBPUSD cotiza, pero no está en la lista
    assert w.engine.risk_status()["server_clock_known"] is False


def test_a_symbol_that_did_not_resolve_is_tried_again_while_the_clock_is_unknown(tmp_path, monkeypatch):
    # Panel o servidor arrancados mientras el terminal aún carga sus símbolos: symbol_info devuelve None para todos.
    # Ese fallo pasajero no puede dejar el símbolo como "no existe" hasta reiniciar el proceso.
    w = World(tmp_path, monkeypatch, symbols=("EURUSD",))
    loaded = w.fake.symbol_info
    monkeypatch.setattr(w.fake, "symbol_info", lambda name: None)
    st = w.engine.auto_status()
    assert st["server_clock_known"] is False and st["can_open"] is False
    assert any("ningún símbolo de `symbols` existe" in b for b in st["blockers"])
    assert any("no hay ningún símbolo de referencia" in b for b in st["blockers"])
    lookups = []
    listed = w.fake.symbols_get
    monkeypatch.setattr(w.fake, "symbols_get", lambda *a, **k: lookups.append(1) or listed(*a, **k))
    for _ in range(6):  # el panel sigue sondeando cada 5 s: el nombre no se vuelve a buscar en cada lectura
        w.t += 5
        assert any("ningún símbolo de `symbols` existe" in b for b in w.engine.auto_status()["blockers"])
    assert lookups == []
    monkeypatch.setattr(w.fake, "symbol_info", loaded)  # el terminal termina de cargar
    w.t += 30  # un minuto después de la primera búsqueda se repite
    assert not any("ningún símbolo de `symbols`" in b for b in w.engine.auto_status()["blockers"])  # ya resuelve; falta ver moverse un tick
    w.t += 5
    st = w.engine.auto_status()
    assert st["server_clock_known"] is True and st["can_open"] is True and st["blockers"] == []
    # con la hora ya conocida, un nombre que no existe se busca una vez y no más
    w.engine.cfg = replace(w.cfg, symbols=("EURUSD", "NOEXISTE"))
    lookups.clear()
    for _ in range(30):
        w.t += 5
        assert w.engine.auto_status()["server_clock_known"] is True
    assert len(lookups) == 1


def test_visible_symbols_are_capped_and_prefer_recent_quotes(fake):
    broker = MT5Broker(module=fake)
    for i in range(30):
        fake.add_symbol(f"SYM{i:02d}", visible=i != 3)
        fake.set_tick(f"SYM{i:02d}", 1.0, 1.1, time=fake.server_now() - 1000 - i)
    names = broker.visible_symbols(20)
    assert len(names) == 20 and names[0] == "EURUSD" and names[1:4] == ["SYM00", "SYM01", "SYM02"] and "SYM03" not in names
    fake.fail_symbols = True
    with pytest.raises(BrokerError, match="symbols_get"):
        broker.visible_symbols()


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
    assert b["forming_bar_may_be_closed"] is False and b["server_clock_known"] is True and "AVISO" not in b["note"]
    assert engine.bars("EURUSD", " m15 ", 10)["timeframe"] == "M15"  # se admite con espacios y en minúsculas


SLOPE = 0.0002  # lo que avanza cada vela de la tendencia de prueba
WICK = 0.0005  # mecha de las velas del doble, por arriba y por abajo


def bars_after_a_candle_against_the_trend(w: World, direction: int, move: float, timeframe: str = "M15") -> tuple[dict, float, float]:
    """Tendencia recta (direction 1 sube, -1 baja), una última vela cerrada de `move` en contra y la vela en formación.

    Devuelve lo que responde bars, el cierre anterior a esa última vela cerrada y el suyo.
    """
    closes = [1.1 + direction * SLOPE * i for i in range(270)]
    before = closes[-1]
    last = before - direction * move
    closes += [last, last + direction * 0.03]  # la vela en formación se va lejos: no debe influir en nada
    w.fake.set_rates("EURUSD", getattr(w.fake, f"TIMEFRAME_{timeframe}"), closes, step=TF_SECONDS[timeframe])
    return w.engine.bars("EURUSD", timeframe, 5), before, last


@pytest.mark.parametrize("direction,move,trend", [(1, 0.0030, "up"), (1, 0.0060, "flat"), (-1, 0.0030, "down"), (-1, 0.0060, "flat")])
def test_bars_indicators_after_a_candle_against_the_trend(tmp_path, monkeypatch, direction, move, trend):
    # Con 0.0030 el cierre queda entre la EMA20 y la EMA50 y la tendencia sigue; con 0.0060 cruza la EMA50 y pasa a flat.
    # Los valores esperados se calculan aquí con fórmulas directas, sin indicators.py: en una recta la EMA de n periodos
    # va (n - 1) / 2 velas por detrás del precio, y la vela nueva entra con peso 2 / (n + 1); el rango verdadero de cada
    # vela es su recorrido más las dos mechas; las medias de Wilder dan 13/14 a lo anterior y 1/14 a la vela nueva.
    w = World(tmp_path, monkeypatch, offset=3 * H)
    b, before, last = bars_after_a_candle_against_the_trend(w, direction, move)
    ind = b["indicators"]
    ema20 = (before - direction * 9.5 * SLOPE) * 19 / 21 + last * 2 / 21
    ema50 = (before - direction * 24.5 * SLOPE) * 49 / 51 + last * 2 / 51
    atr14 = (13 * (SLOPE + 2 * WICK) + move + 2 * WICK) / 14
    rs = 13 * SLOPE / move if direction > 0 else move / (13 * SLOPE)  # 13 velas de SLOPE a favor y una de `move` en contra
    assert b["closed_bars_used"] == 260 and ind["trend"] == trend
    assert ind["last_close"] == pytest.approx(last, abs=1e-6)
    assert ind["ema20"] == pytest.approx(ema20, abs=1e-6) and ind["ema50"] == pytest.approx(ema50, abs=1e-6)
    assert ind["rsi14"] == pytest.approx(100 - 100 / (1 + rs), abs=0.06)
    assert ind["atr14"] == pytest.approx(atr14, abs=1e-6) and ind["atr14_points"] == pytest.approx(atr14 / 0.00001, abs=0.06)
    assert ind["close_minus_ema20_in_atr"] == pytest.approx((last - ema20) / atr14, abs=0.006)
    # Máximo y mínimo de las 20 últimas velas cerradas. A favor de la tendencia lo marca el cierre anterior; en contra,
    # la última vela o, si no llega tan lejos, la apertura de la más antigua de las 20.
    edge = before - direction * 19 * SLOPE
    high20, low20 = (before + WICK, min(last, edge) - WICK) if direction > 0 else (max(last, edge) + WICK, before - WICK)
    assert ind["highest_high_20"] == pytest.approx(high20, abs=1e-6) and ind["lowest_low_20"] == pytest.approx(low20, abs=1e-6)
    # La misma serie en H1, de donde la estrategia lee la tendencia de fondo, da los mismos indicadores.
    assert bars_after_a_candle_against_the_trend(w, direction, move, "H1")[0]["indicators"] == ind


@pytest.mark.parametrize("direction", [1, -1])
def test_bars_last_closed_bar_is_the_candle_before_the_forming_one(tmp_path, monkeypatch, direction):
    w = World(tmp_path, monkeypatch, offset=3 * H)  # 12:00 UTC son las 15:00 del servidor: acaba de cerrar la vela de las 14:45
    b, before, last = bars_after_a_candle_against_the_trend(w, direction, 0.0030)
    bar, forming = b["last_closed_bar"], b["forming_bar"]
    assert bar["time_server"] == "2026-10-21 14:45" and forming["time_server"] == "2026-10-21 15:00"
    assert (bar["open"], bar["close"]) == pytest.approx((before, last), abs=1e-6)
    assert (bar["close"] < bar["open"]) is (direction > 0)  # lo que mira la estrategia: bajista tras subir, alcista tras bajar
    assert (bar["high"], bar["low"]) == pytest.approx((max(before, last) + WICK, min(before, last) - WICK), abs=1e-6)
    assert (forming["open"], forming["close"]) == pytest.approx((last, last + direction * 0.03), abs=1e-6)
    assert b["bars"][-1] == [bar["time_server"], bar["open"], bar["high"], bar["low"], bar["close"]]
    assert len(b["bars"]) == 5 and b["bars"][0][0] == "2026-10-21 13:45"


def test_bars_reference_values_on_an_irregular_series_of_a_two_digit_symbol(tmp_path, monkeypatch):
    # La serie de test_indicators.py en un símbolo de 2 decimales. La EMA20 (44.6354) y el RSI (37.7888) son los de la
    # serie; el ATR (0.5781) es el de las velas del doble con mechas de 0.05, calculado aparte con fracciones exactas.
    # Los tres arrastran la serie entera: solo con las 15 últimas velas el RSI sería 30.2 y el ATR 0.658.
    w = World(tmp_path, monkeypatch)
    w.fake.add_symbol("USOIL", digits=2, point=0.01, tick_size=0.01)
    w.fake.set_tick("USOIL", 43.10, 43.14)
    w.fake.set_rates("USOIL", w.fake.TIMEFRAME_M15, [*CLOSES, 40.0], spread=0.05)  # el último cierre es la vela en formación
    b = w.engine.bars("USOIL", "M15", 5)
    ind = b["indicators"]
    assert b["closed_bars_used"] == len(CLOSES) and ind["ema50"] is None and ind["trend"] == "unknown"
    # Tal como los entrega bars: los precios con un decimal más que el símbolo, el RSI con uno y la distancia a la
    # EMA20, (43.13 - 44.6354) / 0.5781 = -2.604, con dos.
    assert ind["ema20"] == round(REF_EMA20, 3) == 44.635 and ind["rsi14"] == round(REF_RSI14[-1], 1) == 37.8
    assert ind["atr14"] == 0.578 and ind["close_minus_ema20_in_atr"] == -2.6
    # El filtro de spread de la estrategia compara estos dos: ambos van en puntos de este símbolo (0.01).
    assert b["quote"]["spread_points"] == 4.0 and ind["atr14_points"] == 57.8


def test_bars_market_that_does_not_move_has_no_trend(engine, fake):
    # Precio 1.0 para que las medias salgan exactas e iguales al precio: ninguna queda por encima de otra y no hay
    # tendencia. Con otros precios el último bit de cada EMA puede romper el empate.
    fake.set_rates("EURUSD", fake.TIMEFRAME_H1, [1.0] * 280, step=3600)
    ind = engine.bars("EURUSD", "H1")["indicators"]
    assert ind["ema20"] == ind["ema50"] == ind["last_close"] == 1.0 and ind["trend"] == "flat"
    assert ind["rsi14"] == 50.0 and ind["close_minus_ema20_in_atr"] == 0.0


def test_bars_indicators_carry_the_whole_warm_up(tmp_path, monkeypatch):
    # Un escalón de 1.2 a 1.1 hace 70 velas y ningún otro movimiento. La EMA de n periodos aún guarda de él
    # (n - 1) / (n + 1) elevado a 70, y las medias de Wilder 13/14 por cada vela pasada: el ATR sigue algo por encima
    # de las dos mechas y el RSI en 0, porque desde entonces no hubo ninguna subida. Calculados solo con las últimas
    # 60 velas no quedaría rastro del escalón.
    w = World(tmp_path, monkeypatch)
    w.fake.set_rates("EURUSD", w.fake.TIMEFRAME_H1, [1.2] * 195 + [1.1] * 71, step=H)  # el último cierre es la vela en formación
    b = w.engine.bars("EURUSD", "H1", 5)
    ind = b["indicators"]
    assert b["closed_bars_used"] == 260 and ind["last_close"] == 1.1
    assert ind["ema20"] == pytest.approx(1.1 + 0.1 * (19 / 21) ** 70, abs=1e-6)  # 1.100091
    assert ind["ema50"] == pytest.approx(1.1 + 0.1 * (49 / 51) ** 70, abs=1e-6)  # 1.106079
    assert ind["atr14"] == pytest.approx(2 * WICK + 0.1 / 14 * (13 / 14) ** 69, abs=1e-6)  # 0.001043
    assert ind["rsi14"] == 0.0 and ind["trend"] == "down"


def test_bars_count_is_kept_between_5_and_200(engine, fake):
    fake.set_rates("EURUSD", fake.TIMEFRAME_M15, [1.1 + i * 0.0001 for i in range(280)])
    assert [len(engine.bars("EURUSD", "M15", n)["bars"]) for n in (0, 1, 5, 30, 200, 10_000)] == [5, 5, 5, 30, 200, 200]
    assert len(engine.bars("EURUSD", "M15")["bars"]) == 30  # sin count, 30


def test_timeframe_seconds_cover_every_supported_timeframe():
    assert set(TF_SECONDS) == set(TIMEFRAMES)
    assert TF_SECONDS["M1"] == 60 and TF_SECONDS["M15"] == 900 and TF_SECONDS["H4"] == 4 * 3600 and TF_SECONDS["D1"] == 86400


def test_bars_last_row_is_a_closed_candle_when_no_tick_arrived_in_the_new_one(engine, fake):
    # 20 s después del cierre de vela EURUSD aún no ha recibido tick (el último es de hace 30 s): MT5 no ha creado
    # la vela nueva y la última fila es la que acaba de cerrar. GBPUSD sí cotiza, así que la hora del servidor se conoce.
    fake.add_symbol("GBPUSD")
    fake.set_tick("GBPUSD", 1.3, 1.3001)
    fake.freeze_ticks("EURUSD", age=30)
    closes = [1.0500 + i * 0.0002 for i in range(280)]
    closes[-1] = 1.0400  # la vela recién cerrada es bajista
    fake.set_rates("EURUSD", fake.TIMEFRAME_M15, closes, last_open=fake.server_now() - 920)
    b = engine.bars("EURUSD", "M15", 10)
    assert b["server_clock_known"] and b["quote"]["stale"] is False and 29 <= b["quote"]["age_seconds"] <= 32
    assert b["forming_bar"] is None and b["forming_bar_may_be_closed"] is False
    assert b["last_closed_bar"]["close"] == 1.04 and b["indicators"]["last_close"] == 1.04
    assert b["bars"][-1][4] == 1.04 and b["closed_bars_used"] == 261
    assert b["indicators"]["lowest_low_20"] == pytest.approx(1.04 - 0.0005, abs=1e-6)  # los indicadores la incluyen
    # con un tick en la vela nueva, la última fila vuelve a ser la vela en formación
    fake.set_rates("EURUSD", fake.TIMEFRAME_M15, [*closes, 1.0401], last_open=fake.server_now() - 20)
    b2 = engine.bars("EURUSD", "M15", 10)
    assert b2["forming_bar"]["close"] == 1.0401 and b2["last_closed_bar"]["close"] == 1.04


def test_bars_candle_closes_exactly_at_its_end(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, offset=2 * H)
    w.fake.freeze_ticks("EURUSD")
    closes = [1.1 + i * 0.0001 for i in range(60)]
    w.fake.set_rates("EURUSD", w.fake.TIMEFRAME_H1, closes, step=3600, last_open=w.fake.server_now() - 3600 + 2)
    assert w.engine.bars("EURUSD", "H1")["forming_bar"] is not None  # el reloj se fija en 1 s: aún falta 1 s para el cierre
    w.t += 1
    assert w.engine.bars("EURUSD", "H1")["forming_bar"] is None


def test_bars_with_the_market_closed_and_unknown_server_clock(engine, fake):
    closes = [1.0500 + i * 0.0002 for i in range(280)]
    closes[-1] = 1.2000
    # Mercado cerrado hace 2 horas: ningún tick se mueve y la hora del servidor no se conoce. La última vela cerró,
    # pero sin reloj no se puede asegurar: se avisa en vez de afirmar que está en formación.
    fake.freeze_ticks(age=2 * H)
    fake.set_rates("EURUSD", fake.TIMEFRAME_M15, closes, last_open=fake.server_now() - 2 * H - 600)
    b = engine.bars("EURUSD", "M15", 10)
    assert b["server_clock_known"] is False and b["quote"]["stale"] is True
    assert b["forming_bar"]["close"] == 1.2 and b["forming_bar_may_be_closed"] is True and "AVISO" in b["note"]
    assert b["indicators"]["last_close"] == pytest.approx(closes[-2], abs=1e-6)


def test_bars_with_the_market_closed_long_ago_count_the_last_candle(engine, fake):
    closes = [1.0500 + i * 0.0002 for i in range(280)]
    closes[-1] = 1.2000
    # Cerrado hace 13 horas: aun sin conocer la hora del servidor, ningún servidor va más de 12 h por detrás de UTC,
    # así que la última vela (terminó hace más de 12 h en cualquier huso) está cerrada con seguridad.
    fake.freeze_ticks(age=13 * H)
    fake.set_rates("EURUSD", fake.TIMEFRAME_M15, closes, last_open=fake.server_now() - 13 * H - 900)
    b = engine.bars("EURUSD", "M15", 10)
    assert b["server_clock_known"] is False
    assert b["forming_bar"] is None and b["forming_bar_may_be_closed"] is False and "AVISO" not in b["note"]
    assert b["last_closed_bar"]["close"] == 1.2 and b["indicators"]["last_close"] == 1.2 and b["closed_bars_used"] == 261


def test_bars_unknown_clock_uses_the_newest_tick_as_proof_that_the_candle_closed(tmp_path, monkeypatch):
    # PC 45 s atrasado: la hora del servidor queda desconocida aunque el mercado cotiza. Un tick de GBPUSD posterior
    # al final de la vela de EURUSD prueba que esa vela terminó.
    w = World(tmp_path, monkeypatch, offset=3 * H)
    monkeypatch.setattr(w.engine, "_now", lambda: datetime.fromtimestamp(w.t - 45, tz=timezone.utc))
    w.fake.freeze_ticks("EURUSD", age=30)
    closes = [1.1 + i * 0.0001 for i in range(60)]
    w.fake.set_rates("EURUSD", w.fake.TIMEFRAME_M15, closes, last_open=w.fake.server_now() - 920)
    b = w.engine.bars("EURUSD", "M15")
    assert b["server_clock_known"] is False and b["forming_bar"] is None and b["forming_bar_may_be_closed"] is False
    assert b["last_closed_bar"]["close"] == pytest.approx(closes[-1])


def test_bars_downtrend_and_short_history(engine, fake):
    fake.set_rates("EURUSD", fake.TIMEFRAME_H1, [1.2 - i * 0.0002 for i in range(280)], step=3600)
    assert engine.bars("EURUSD", "H1")["indicators"]["trend"] == "down"
    fake.set_rates("EURUSD", fake.TIMEFRAME_M5, [1.1, 1.1001, 1.1002, 1.1001, 1.1], step=300)
    short = engine.bars("EURUSD", "M5", 50)
    assert short["indicators"]["ema50"] is None and short["indicators"]["trend"] == "unknown" and len(short["bars"]) == 4
    assert short["indicators"]["rsi14"] is None and short["indicators"]["atr14"] is None
    # 17 velas cerradas: ya hay RSI y ATR, pero no EMA20, así que tampoco distancia a la EMA20 ni tendencia
    fake.set_rates("EURUSD", fake.TIMEFRAME_M5, [1.1 + i * 0.0001 for i in range(18)], step=300)
    some = engine.bars("EURUSD", "M5")["indicators"]
    assert some["rsi14"] == 100.0 and some["atr14"] == pytest.approx(0.0011, abs=1e-6)
    assert some["ema20"] is None and some["close_minus_ema20_in_atr"] is None and some["trend"] == "unknown"
    # velas sin ningún recorrido: el ATR es 0 y la distancia a la EMA20 queda en None, sin dividir por cero
    fake.set_rates("EURUSD", fake.TIMEFRAME_M5, [1.1] * 40, spread=0.0, step=300)
    still = engine.bars("EURUSD", "M5")["indicators"]
    assert still["atr14"] == 0.0 and still["rsi14"] == 50.0 and still["close_minus_ema20_in_atr"] is None


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
