"""Las tres piezas juntas sobre el mismo terminal de mentira y un mismo reloj.

- El bucle (`autoloop.loop`), con su comprobación previa sobre un Engine nuevo en cada ciclo.
- El servidor MCP del ciclo: otro Engine nuevo, como el que arranca `claude -p`.
- El panel: un Monitor de larga vida con el motor del modo real (sin espera de reloj ni parada enclavada).

Cada pieza tiene sus pruebas; aquí se comprueba que no se contradicen entre sí.
"""

import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from conftest import WHY, World

from mt5_mcp import autoloop, dashboard
from mt5_mcp.auto import ENABLE_HINT
from mt5_mcp.broker import MT5Broker
from mt5_mcp.config import AutoConfig
from mt5_mcp.engine import Engine
from mt5_mcp.journal import Journal

REPO = Path(__file__).resolve().parents[1]
FAKE_CLAUDE = [sys.executable, str(REPO / "tests" / "fake_claude.py")]
TOML = 'symbols = ["EURUSD", "GBPUSD"]\nmax_volume_per_order = 1.0\nmax_total_volume = 3.0\n\n[auto]\nenabled = true\n'
H = 3600


class Site:
    """Un PC con el terminal abierto, el panel en una ventana y el bucle listo para lanzarse."""

    def __init__(self, tmp_path, monkeypatch):
        self.w = World(tmp_path, monkeypatch)
        self.root = tmp_path
        (tmp_path / "risk.toml").write_text(TOML, encoding="utf-8")
        (tmp_path / "auto_protocol.md").write_text("# Protocolo\nLlama a auto_status.\n", encoding="utf-8")
        (tmp_path / "strategy.md").write_text("# Estrategia\nCompra si H1 sube.\n", encoding="utf-8")
        self.record = tmp_path / "launched.json"
        monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(self.record))
        monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
        monkeypatch.setattr(dashboard, "CACHE_SECONDS", -1.0)  # cada sondeo del panel es una lectura nueva
        panel = Engine(MT5Broker(module=self.w.fake), cfg=self.w.cfg, root=tmp_path, journal=Journal(tmp_path / "journal" / "trades.jsonl"),
                       latch_halt=False, clock_wait=0.0)
        monkeypatch.setattr(panel, "_now", lambda: datetime.fromtimestamp(self.w.t, tz=timezone.utc))
        # el panel mide la separación entre sondeos con el mismo reloj simulado (tras una pausa larga, "comprobando")
        self.monitor = dashboard.Monitor(panel, monotonic=lambda: self.w.t, wall=lambda: self.w.t)

    def panel(self) -> dict:
        """Lo que muestra la página tras dos sondeos seguidos (uno cada 5 s)."""
        self.monitor.state()
        self.w.t += 5
        return self.monitor.state()

    def server(self) -> Engine:
        """El servidor MCP de un ciclo: un proceso nuevo."""
        return self.w.new_engine()

    def loop(self, *args: str) -> tuple[int, list[str], bool]:
        """Lanza el bucle con esos argumentos. Devuelve (código de salida, líneas de la consola, ¿llegó a lanzar Claude?)."""
        self.record.unlink(missing_ok=True)
        out: list[str] = []
        naps = []

        def sleep(seconds: float) -> None:
            # En el reloj simulado una espera no cuesta nada: un bucle que no viera su final (HALT, --once,
            # --max-cycles) dormiría para siempre y colgaría la suite. Con tope, la prueba falla enseguida.
            naps.append(seconds)
            assert len(naps) < 10_000, "el bucle sigue esperando ciclos: no ha visto su final"
            self.w.sleep(seconds)

        code = autoloop.loop(autoloop.parse_args(list(args)), self.root, out.append, sleep=sleep, now=lambda: self.w.t,
                             engine_factory=self.w.new_engine, claude=FAKE_CLAUDE)
        return code, out, self.record.exists()


def test_panel_loop_and_cycle_server_tell_the_same_story_on_a_normal_day(tmp_path, monkeypatch):
    s = Site(tmp_path, monkeypatch)
    code, out, launched = s.loop("--once")
    assert code == 0 and launched and "Hay trabajo: el bot puede buscar una entrada" in out
    server = s.server()
    assert server.auto_status()["can_open"] is True
    assert server.auto_order("EURUSD", "buy", 1.0981, 1.1041, WHY)["sent"]  # lo que haría Claude en ese ciclo
    state = s.panel()
    auto = server.auto_status()
    assert state["auto"]["trades_today"] == auto["trades_today"] == 1 and [p["opened_by"] for p in state["positions"]] == ["auto"]
    assert state["auto"]["can_open"] is False and auto["can_open"] is False
    assert [b.split(" desde")[0] for b in state["auto"]["blockers"]] == [b.split(" desde")[0] for b in auto["blockers"]] == ["enfriamiento: faltan 15 min"]
    assert [e["event"] for e in state["journal"]] == ["auto_order_attempt", "auto_order_sent"]
    # siguiente vela: no puede abrir (enfriamiento), pero tiene una posición que revisar
    s.w.advance(14)
    code, out, launched = s.loop("--once")
    assert code == 0 and launched and "Hay trabajo: 1 posición(es) del bot que revisar" in out
    server = s.server()
    s.w.fake.close_profit = -40.0
    assert server.auto_close(server.auto_status()["open_auto_positions"][0]["ticket"], WHY)["closed"]
    state, auto = s.panel(), s.server().auto_status()
    for key in ("trades_today", "closed_today", "wins_today", "consecutive_losses", "realized_today_auto", "can_open", "blockers"):
        assert state["auto"][key] == auto[key], key
    assert auto["closed_today"] == 1 and auto["consecutive_losses"] == 1 and auto["realized_today_auto"] == -40.0 and state["positions"] == []
    assert state["risk"]["realized_today"] == -40.0 and state["risk"]["server_clock_known"] is True


def test_closed_market_is_seen_the_same_by_the_loop_the_cycle_server_and_the_panel(tmp_path, monkeypatch):
    s = Site(tmp_path, monkeypatch)
    assert s.panel()["auto"]["can_open"] is True
    s.w.fake.freeze_ticks()  # viernes por la noche: el mercado cierra con el panel abierto
    panel_knows_the_clock = []
    for hours in (0.25, 1, 6, 13, 30):  # antes y después de que caduque la hora que el panel recordaba (12 h)
        s.w.t += hours * H
        state = s.panel()
        code, out, launched = s.loop("--once")
        auto = s.server().auto_status()
        # el bucle no gasta un ciclo, y ni el servidor ni el panel dicen que el bot pueda abrir
        assert code == 0 and not launched and any(line.startswith("Sin nada que hacer: hora del servidor desconocida") for line in out)
        assert auto["can_open"] is False and state["auto"]["can_open"] is False and state["clock_settling"] is False
        assert len(state["auto"]["blockers"]) == 1 and len(auto["blockers"]) == 1
        panel_knows_the_clock.append(state["auto"]["server_clock_known"])
        if state["auto"]["server_clock_known"]:  # el panel aún recuerda la hora: el motivo es que nada cotiza
            assert state["auto"]["blockers"][0].startswith("sin cotizaciones recientes en los símbolos de `symbols`")
        else:
            assert state["auto"]["blockers"] == auto["blockers"] and "no llega ninguna cotización nueva" in auto["blockers"][0]
        assert s.server().auto_order("EURUSD", "buy", 1.0981, 1.1041, WHY)["sent"] is False
    assert panel_knows_the_clock == [True, True, True, False, False]
    assert not s.w.fake.sent and not s.w.fake.checked
    s.w.fake.touch_ticks()  # domingo por la noche: reabre
    s.w.t += 60
    code, out, launched = s.loop("--once")
    assert code == 0 and launched and "Hay trabajo: el bot puede buscar una entrada" in out
    s.panel()  # el panel ve llegar la primera cotización tras el parón y espera a otra para fiarse de su hora
    state = s.panel()
    assert state["auto"]["server_clock_known"] is True and state["auto"]["can_open"] is True and state["auto"]["blockers"] == []


def test_waking_the_pc_with_the_market_closed_does_not_blame_the_windows_clock(tmp_path, monkeypatch):
    """PC suspendido con el panel abierto y el bucle esperando; se reanuda 14 h después, con el mercado cerrado.

    Al reconectar, el terminal sustituye su último tick por el último del servidor, que tiene 12 h: el tick "se mueve"
    sin ser nuevo y su hora no encaja con la del PC. El reloj del PC es correcto, así que ni el panel ni el bucle
    pueden dar la hora de Windows como única causa. No se abre nada ni se lanza Claude.
    """
    s = Site(tmp_path, monkeypatch)
    assert s.server().auto_order("EURUSD", "buy", 1.0981, 1.1041, WHY)["sent"]
    assert s.panel()["auto"]["server_clock_known"] is True
    suspended = s.w.t
    s.w.fake.freeze_ticks()  # el PC se suspende: el terminal se queda con los ticks de ese momento
    s.w.t += 14 * H + 7 * 60 + 13  # despierta
    assert s.monitor.state()["clock_settling"] is True  # primera lectura del panel tras la pausa: aún sin veredicto

    def reconnect():  # el último tick del servidor, de casi 2 h después de la suspensión: aun así, de hace 12 h
        for name in ("EURUSD", "GBPUSD"):
            s.w.fake.ticks[name] = s.w.fake.ticks[name]._replace(time=int(suspended) + 2 * H - 2)

    naps = []

    def sleep(seconds):  # el terminal reconecta mientras el motor del bucle espera a ver moverse una cotización
        naps.append(seconds)
        World.sleep(s.w, seconds)
        if len(naps) == 2:
            reconnect()

    monkeypatch.setattr(s.w, "sleep", sleep)
    both_causes = ("hora del servidor desconocida: llegan cotizaciones, pero su hora no encaja con el reloj de este PC: o son "
                   "antiguas (el terminal acaba de reconectar con el mercado parado) o la hora de Windows está desajustada")
    code, out, launched = s.loop("--once")
    skip = [line for line in out if line.startswith("Sin nada que hacer: ")]
    assert code == 0 and not launched and len(naps) == 2  # deja de esperar en cuanto ve la cotización que no encaja
    assert len(skip) == 1 and skip[0].startswith("Sin nada que hacer: " + both_causes)
    assert "Queda 1 posición del bot abierta sin revisar mientras no se conozca la hora del servidor" in skip[0]
    s.w.t += 2.5  # segunda lectura del panel, con el tick nuevo ya en el terminal
    state = s.monitor.state()
    clock = [b for b in state["auto"]["blockers"] if b.startswith("hora del servidor desconocida")]
    assert state["clock_settling"] is False and state["auto"]["can_open"] is False and len(clock) == 1
    assert clock[0].startswith(both_causes)
    for text in (skip[0], clock[0]):
        assert "reloj de este PC; sincroniza" not in text  # el texto anterior daba la hora de Windows por única causa
    assert len(s.w.fake.sent) == 1  # solo la orden de antes de la suspensión


def test_a_quoting_symbol_outside_the_list_does_not_make_the_bot_look_able_to_open(tmp_path, monkeypatch):
    # Fin de semana con una posición manual en un símbolo que sí cotiza (cripto) y no está en la lista: gracias a él se
    # conoce la hora del servidor, pero el bot solo abre en los de la lista, que están parados.
    s = Site(tmp_path, monkeypatch)
    fake = s.w.fake
    fake.add_symbol("BTCUSD", digits=2, point=0.01, tick_size=0.01)
    fake.set_tick("BTCUSD", 60000.0, 60010.0)
    fake.add_position("BTCUSD", "buy", 0.01, 60000.0, sl=59000.0)
    assert s.panel()["auto"]["can_open"] is True
    fake.freeze_ticks("EURUSD", "GBPUSD")
    s.w.t += 20 * H
    quiet = "sin cotizaciones recientes en los símbolos de `symbols`: la última llegó hace 20 h (máximo 60 s); mercado parado o sin ticks"
    auto = s.server().auto_status()  # proceso nuevo: la hora la da BTCUSD, que se mueve
    assert auto["server_clock_known"] is True and auto["can_open"] is False and auto["blockers"] == [quiet]
    state = s.panel()
    assert state["auto"]["server_clock_known"] is True and state["auto"]["blockers"] == [quiet]
    code, out, launched = s.loop("--once")
    assert code == 0 and not launched and f"Sin nada que hacer: {quiet}" in out
    for symbol, sl, tp in (("EURUSD", 1.0981, 1.1041), ("GBPUSD", 1.2981, 1.3041)):  # y el servidor rechazaría cualquiera de la lista
        assert s.server().auto_order(symbol, "buy", sl, tp, WHY)["sent"] is False
    assert not fake.sent
    # con el modo automático apagado ese aviso no se añade: el estado solo dice cómo activarlo
    off = Engine(MT5Broker(module=fake), cfg=replace(s.w.cfg, auto=AutoConfig(enabled=False)), root=tmp_path, sleep=s.w.sleep)
    monkeypatch.setattr(off, "_now", lambda: datetime.fromtimestamp(s.w.t, tz=timezone.utc))
    assert off.auto_status()["blockers"] == [ENABLE_HINT]


def test_stop_button_of_the_panel_stops_the_loop_and_leaves_the_cycle_server_inert(tmp_path, monkeypatch):
    s = Site(tmp_path, monkeypatch)
    running = s.server()  # servidor de un ciclo en marcha
    assert running.auto_order("EURUSD", "buy", 1.0981, 1.1041, WHY)["sent"]
    ticket = running.positions()[0]["ticket"]
    assert s.panel()["auto"]["can_manage_positions"] is True
    assert s.monitor.halt()["halted"] is True  # botón "Detener el bot"
    assert (s.root / "HALT").exists() and len(s.w.fake.sent) == 1  # crea el archivo y no cierra nada
    state = s.panel()
    assert state["risk"]["halted"] is True and state["auto"]["can_open"] is False and state["auto"]["can_manage_positions"] is False
    assert state["journal"][-1]["event"] == "halt" and len(state["positions"]) == 1
    # el ciclo que estaba en marcha ya no puede tocar nada, ni siquiera su propia posición
    assert running.auto_close(ticket, WHY)["closed"] is False and running.auto_tighten_stop(ticket, 1.0990, WHY)["modified"] is False
    assert running.auto_order("GBPUSD", "buy", 1.2981, 1.3041, WHY)["sent"] is False and len(s.w.fake.sent) == 1
    # el bucle termina sin lanzar más ciclos, y su comprobación previa da el mismo motivo
    code, out, launched = s.loop("--max-cycles", "3")
    assert code == 0 and not launched and out[-1].startswith("Existe el archivo HALT: el bucle termina")
    run, why = autoloop.precheck(s.w.new_engine)
    assert run is False and "kill switch activo" in why and "Queda 1 posición del bot abierta sin revisar" in why
    # el usuario reanuda borrando HALT: el panel lo refleja sin reiniciarse; el servidor que lo vio sigue parado
    # hasta que se reinicia, y el bucle vuelve a trabajar cuando se lanza de nuevo
    (s.root / "HALT").unlink()
    state = s.panel()
    assert state["risk"]["halted"] is False and state["auto"]["can_manage_positions"] is True
    assert running.auto_status()["can_manage_positions"] is False
    assert s.server().auto_status()["can_manage_positions"] is True
    code, out, launched = s.loop("--once")
    assert code == 0 and launched and "Hay trabajo: 1 posición(es) del bot que revisar" in out
