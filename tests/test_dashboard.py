"""Panel en vivo: servidor HTTP local, solo lectura salvo el botón de parada."""

import importlib
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from mt5_mcp import config, dashboard
from mt5_mcp.broker import BrokerError, MT5Broker
from mt5_mcp.config import AutoConfig, RiskConfig
from mt5_mcp.engine import Engine
from mt5_mcp.journal import Journal

WHY = "tendencia alcista en H1 y retroceso a la EMA20 en M15"
TOML = 'symbols = ["EURUSD"]\nmax_volume_per_order = 1.0\nmax_total_volume = 3.0\n'


def panel_engine(fake, root, **cfg):
    """Engine como el del panel en modo real: no enclava la parada ni espera a que se mueva una cotización."""
    base = dict(symbols=("EURUSD",), max_volume_per_order=1.0, max_total_volume=3.0, auto=AutoConfig(enabled=True))
    base.update(cfg)
    return Engine(MT5Broker(module=fake), cfg=RiskConfig(**base), root=root, journal=Journal(root / "journal" / "trades.jsonl"),
                  latch_halt=False, clock_wait=0.0)


@pytest.fixture
def panel(fake, tmp_path):
    engine = panel_engine(fake, tmp_path)
    monitor = dashboard.Monitor(engine)
    httpd = dashboard.serve(monitor, 0)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    yield {"url": f"http://127.0.0.1:{port}", "port": port, "monitor": monitor, "engine": engine, "fake": fake, "root": tmp_path}
    httpd.shutdown()
    httpd.server_close()


def call(url, method="GET", headers=None):
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            return res.status, res.read().decode("utf-8"), res.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), exc.headers


def get_state(panel):
    return json.loads(call(panel["url"] + "/api/state")[1])


def halt(panel):
    return call(panel["url"] + "/api/halt", "POST", {"X-Panel-Token": panel["monitor"].token})


def fresh(monitor, fake, seconds=1.0):
    """Siguiente sondeo de la página: la caché ha caducado y, si el mercado cotiza, han llegado cotizaciones nuevas."""
    fake.sleep(seconds)
    monitor._cached = None


def settled(panel):
    """Segunda lectura del panel, con la hora del servidor ya confirmada."""
    get_state(panel)
    fresh(panel["monitor"], panel["fake"])
    return get_state(panel)


def test_page_and_state(panel):
    code, html, headers = call(panel["url"] + "/")
    assert code == 200 and "Panel en vivo" in html and panel["monitor"].token in html and "__PANEL_TOKEN__" not in html
    assert headers["X-Frame-Options"] == "DENY" and "default-src 'self'" in headers["Content-Security-Policy"]
    assert "innerHTML" not in html  # los datos del journal se pintan siempre como texto
    code, body, _ = call(panel["url"] + "/api/state")
    state = json.loads(body)
    assert code == 200 and state["ok"] and state["demo"] is False
    assert state["account"]["mode"] == "DEMO" and state["auto"]["enabled"] and state["risk"]["open_positions"] == 0
    assert state["account"]["terminal_connected"] is True
    assert state["equity_series"][-1][1] == 10_000.0 and state["positions"] == []


def test_page_uses_what_the_state_reports():
    html = dashboard.PAGE.read_text(encoding="utf-8")
    # conexión del terminal, límite diario alcanzado, error de risk.toml, primera lectura y HALT con MT5 caído
    for field in ("a.terminal_connected", "r.daily_loss_hit", "s.config_error", "s.clock_settling", "s.halted"):
        assert field in html
    assert "daily_loss_limit) || 1" not in html  # un límite diario de 0 se muestra tal cual, sin sustituirlo por otro
    assert "setInterval(poll" not in html  # un sondeo no empieza hasta que termina el anterior
    # el aviso de parada habla del archivo HALT (el panel no sabe más que si existe) y dice cómo reanudar
    assert "El archivo HALT existe" in html and "Para reanudar, borra el archivo HALT" in html
    # sin hora del servidor, los importes y contadores "de hoy" se marcan como no verificados (el estado trae el dato)
    assert 'r.server_clock_known === false ? " · día sin verificar"' in html


def test_first_read_never_waits_and_the_second_settles_the_server_clock(panel):
    slept = []
    panel["engine"]._sleep = slept.append
    first = get_state(panel)
    # una sola lectura no puede confirmar la hora del servidor: la página lo presenta como "comprobando", no como bloqueo
    assert first["ok"] and first["auto"]["server_clock_known"] is False and first["clock_settling"] is True
    second = settled(panel)
    assert second["auto"]["server_clock_known"] is True and second["clock_settling"] is False
    assert second["auto"]["can_open"] is True and second["auto"]["blockers"] == []
    assert slept == []  # la primera carga no se queda esperando a que se mueva una cotización


def test_closed_market_is_a_verdict_from_the_second_read(panel, monkeypatch):
    panel["fake"].freeze_ticks()
    assert get_state(panel)["clock_settling"] is True
    fresh(panel["monitor"], panel["fake"])
    state = get_state(panel)
    assert state["auto"]["server_clock_known"] is False and state["clock_settling"] is False
    assert state["risk"]["server_clock_known"] is False and "SIN VERIFICAR" in state["risk"]["day_window"]
    assert state["auto"]["cooldown_remaining_minutes"] is None  # sin hora no hay enfriamiento que mostrar
    # el servidor ya explica el motivo en sus bloqueos; la página reconoce ese texto y no añade su aviso genérico
    assert any("hora del servidor" in b for b in state["auto"]["blockers"])
    assert '"hora del servidor"' in dashboard.PAGE.read_text(encoding="utf-8")
    # tras una pausa larga sin lecturas vuelve a hacer falta una segunda
    monkeypatch.setattr(dashboard, "CLOCK_GAP_SECONDS", -1.0)
    fresh(panel["monitor"], panel["fake"])
    assert get_state(panel)["clock_settling"] is True


def test_panel_left_open_says_when_the_listed_symbols_stop_quoting(panel, monkeypatch):
    # El panel queda abierto al cerrar el mercado: su motor sigue conociendo la hora del servidor durante horas, así que
    # el aviso no puede depender de "hora desconocida". El bucle (proceso nuevo en cada ciclo) ya salta esos ciclos.
    monkeypatch.setattr(panel["engine"], "_now", panel["fake"].utcnow)  # el motor y el doble comparten reloj
    assert settled(panel)["auto"]["can_open"] is True
    panel["fake"].freeze_ticks()
    fresh(panel["monitor"], panel["fake"], seconds=10 * 60)
    state = get_state(panel)
    assert state["clock_settling"] is False and state["auto"]["server_clock_known"] is True and state["auto"]["can_open"] is False
    assert state["auto"]["blockers"] == [
        "sin cotizaciones recientes en los símbolos de `symbols`: la última llegó hace 10 min (máximo 60 s); mercado parado o sin ticks"]
    assert state["risk"]["blockers"] == []  # las órdenes con confirmación no dependen de esto: las juzga order_check
    panel["fake"].touch_ticks()  # reabre
    fresh(panel["monitor"], panel["fake"])
    assert get_state(panel)["auto"]["blockers"] == []


def test_page_recognises_the_blockers_it_special_cases(fake, tmp_path):
    """La página trata aparte algunos bloqueos por su texto: si el servidor los redacta de otro modo, debe verse aquí."""
    html = dashboard.PAGE.read_text(encoding="utf-8")
    for n, (symbols, marker) in enumerate([((), "lista `symbols` no vacía"), (("EURUSD.x",), "ningún símbolo de `symbols`")]):
        monitor = dashboard.Monitor(panel_engine(fake, tmp_path / str(n), symbols=symbols))
        monitor.state()
        fresh(monitor, fake)
        auto = monitor.state()["auto"]
        assert auto["can_open"] is False and any(marker in b for b in auto["blockers"])
        assert f'"{marker}"' in html  # con ese motivo a la vista, el aviso genérico sobre la hora del servidor no se muestra
    # enfriamiento: la página sustituye los minutos del bloqueo por los de la tarjeta (nunca "0 min")
    engine = panel_engine(fake, tmp_path / "cooldown")
    monitor = dashboard.Monitor(engine)
    monitor.state()
    fresh(monitor, fake)
    assert engine.auto_order("EURUSD", "buy", 1.0981, 1.1041, WHY)["sent"] is True
    fresh(monitor, fake)
    auto = monitor.state()["auto"]
    assert auto["cooldown_remaining_minutes"] > 0 and any(b.startswith("enfriamiento: faltan ") for b in auto["blockers"])
    assert "/^enfriamiento: faltan " in html


def test_state_reports_terminal_errors_without_crashing(panel):
    panel["fake"].fail_positions = True
    panel["monitor"]._cached = None
    code, body, _ = call(panel["url"] + "/api/state")
    state = json.loads(body)
    assert code == 200 and state["ok"] is False and "positions_get" in state["error"] and state["halted"] is False
    (panel["root"] / "HALT").write_text("parada puesta a mano\n", encoding="utf-8")
    panel["monitor"]._cached = None
    assert get_state(panel)["halted"] is True  # el archivo HALT se ve aunque MetaTrader 5 no responda
    (panel["root"] / "HALT").unlink()
    panel["fake"].fail_positions = False
    panel["monitor"]._cached = None
    assert json.loads(call(panel["url"] + "/api/state")[1])["ok"] is True


def test_state_error_is_not_masked_by_cached_state(panel, monkeypatch):
    assert get_state(panel)["ok"] is True
    panel["fake"].fail_positions = True
    monkeypatch.setattr(dashboard, "CACHE_SECONDS", 0.0)  # caduca la caché sin borrarla a mano
    state = get_state(panel)
    assert state["ok"] is False and "positions_get" in state["error"]


def test_reading_the_panel_never_writes(panel):
    for _ in range(3):
        panel["monitor"]._cached = None
        call(panel["url"] + "/api/state")
    assert not panel["fake"].sent and not panel["fake"].checked
    assert panel["engine"].journal_tail() == [] and not (panel["root"] / "HALT").exists()


def test_halt_button_needs_token_and_only_blocks(panel):
    panel["fake"].add_position("EURUSD", "buy", 0.1, 1.0990, sl=1.0970)
    assert call(panel["url"] + "/api/halt", "POST")[0] == 403
    assert call(panel["url"] + "/api/halt", "POST", {"X-Panel-Token": "malo"})[0] == 403
    assert call(panel["url"] + "/api/halt", "POST", {"X-Panel-Token": "contraseña"})[0] == 403  # no ASCII: rechazo, no un fallo
    assert call(panel["url"] + "/api/halt", "GET")[0] == 404
    assert not (panel["root"] / "HALT").exists()
    code, body, _ = halt(panel)
    assert code == 200 and json.loads(body)["halted"] is True and (panel["root"] / "HALT").exists()
    assert not panel["fake"].sent and len(panel["engine"].positions()) == 1  # no cierra posiciones
    state = json.loads(call(panel["url"] + "/api/state")[1])
    assert state["risk"]["halted"] is True and state["auto"]["can_open"] is False


def test_halt_notice_follows_the_file_without_restarting_the_panel(panel):
    assert settled(panel)["auto"]["can_open"] is True
    assert halt(panel)[0] == 200
    state = get_state(panel)
    assert state["risk"]["halted"] is True and state["auto"]["can_open"] is False
    (panel["root"] / "HALT").unlink()  # el usuario reanuda: borra el archivo (el servidor MCP que reinicia es otro proceso)
    fresh(panel["monitor"], panel["fake"])
    state = get_state(panel)
    assert state["risk"]["halted"] is False and state["auto"]["can_open"] is True and state["auto"]["blockers"] == []
    assert halt(panel)[0] == 200 and (panel["root"] / "HALT").exists()  # el botón vuelve a servir
    assert get_state(panel)["risk"]["halted"] is True


def test_halt_does_not_wait_for_a_slow_read(panel):
    monitor, journal = panel["monitor"], panel["engine"].journal
    reading, release, tail = threading.Event(), threading.Event(), journal.tail

    def slow_tail(n=20):  # la lectura se queda parada al final, con la foto del terminal ya tomada
        reading.set()
        release.wait(10)
        return tail(n)

    journal.tail = slow_tail
    reader = threading.Thread(target=monitor.state, daemon=True)
    reader.start()
    assert reading.wait(5)
    try:
        code, body, _ = halt(panel)
        assert code == 200 and json.loads(body)["ok"] is True
        assert (panel["root"] / "HALT").exists() and reader.is_alive()  # HALT creado con la lectura aún en curso
    finally:
        release.set()
        reader.join(5)
    # la foto que empezó antes de la parada no se queda en la caché: el siguiente sondeo ya ve HALT
    assert get_state(panel)["risk"]["halted"] is True


def test_a_slow_read_is_cached_from_when_it_ends(fake, tmp_path, monkeypatch):
    reads = []

    def slow_poll():
        reads.append(1)
        time.sleep(0.6)

    monkeypatch.setattr(dashboard, "CACHE_SECONDS", 0.5)
    monitor = dashboard.Monitor(panel_engine(fake, tmp_path), on_poll=slow_poll)
    monitor.state()
    monitor.state()
    assert len(reads) == 1  # contada desde antes de leer, la caché nacería caducada y cada sondeo repetiría la espera


def test_rejects_foreign_host_and_unknown_paths(panel):
    assert call(panel["url"] + "/api/state", headers={"Host": "evil.example"})[0] == 403
    assert call(panel["url"] + "/", headers={"Host": f"evil.example:{panel['port']}"})[0] == 403
    assert call(panel["url"] + "/api/halt", "POST", {"Host": "evil.example", "X-Panel-Token": panel["monitor"].token})[0] == 403
    assert call(panel["url"] + "/risk.toml")[0] == 404 and call(panel["url"] + "/../risk.toml")[0] == 404
    assert call(f"http://localhost:{panel['port']}/api/state")[0] == 200


def test_server_binds_to_loopback_only(panel):
    httpd = dashboard.serve(panel["monitor"], 0)
    try:
        assert httpd.server_address[0] == "127.0.0.1"
    finally:
        httpd.server_close()


def test_a_second_panel_on_the_same_port_fails(panel, capsys, monkeypatch):
    with pytest.raises(OSError):
        dashboard.serve(panel["monitor"], panel["port"])
    assert dashboard.main(["--demo", "--no-open", "--port", str(panel["port"])]) == 1
    assert f"No se pudo abrir el puerto {panel['port']}" in capsys.readouterr().out
    # serve() usa PanelServer y es su opción la que decide si el socket lleva SO_REUSEADDR
    monkeypatch.setattr(dashboard.PanelServer, "allow_reuse_address", False)
    httpd = dashboard.serve(panel["monitor"], 0)
    try:
        assert httpd.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 0
    finally:
        httpd.server_close()


def test_windows_does_not_reuse_a_port_in_use(monkeypatch):
    # En Windows SO_REUSEADDR deja abrir un puerto que ya usa otro panel; el valor se decide al cargar el módulo
    monkeypatch.setattr(sys, "platform", "win32")
    try:
        assert importlib.reload(dashboard).PanelServer.allow_reuse_address is False
    finally:
        monkeypatch.undo()
        importlib.reload(dashboard)
    assert dashboard.PanelServer.allow_reuse_address is (sys.platform != "win32")


def test_real_mode_follows_risk_toml_and_the_halt_file(fake, tmp_path, monkeypatch):
    toml = tmp_path / "risk.toml"
    toml.write_text(TOML, encoding="utf-8")
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", toml)
    monkeypatch.setattr("mt5_mcp.engine.PROJECT_ROOT", tmp_path)  # HALT y journal de la prueba, no los del repositorio
    brokers = []
    monkeypatch.setattr(dashboard, "MT5Broker", lambda **kw: brokers.append(kw) or MT5Broker(module=fake, **kw))
    engine, on_poll = dashboard._real_engine()
    assert engine.root == tmp_path and brokers == [{"attach_only": True}]  # se une al terminal abierto, sin credenciales
    slept = []
    engine._sleep = slept.append
    monitor = dashboard.Monitor(engine, on_poll=on_poll)

    def state():
        monitor._cached = None
        return monitor.state()

    first = state()
    assert first["ok"] and first["auto"]["enabled"] is False and first["auto"]["max_trades_per_day"] == 4
    assert first["risk"]["daily_loss_limit"] == -300.0 and slept == []
    # el usuario activa el bot y cambia límites con el panel abierto
    toml.write_text('journal_file = "journal/otro.jsonl"\nmax_daily_loss_pct = 1.0\n' + TOML + "[auto]\nenabled = true\nmax_trades_per_day = 2\n",
                    encoding="utf-8")
    edited = state()
    assert edited["auto"]["enabled"] is True and edited["auto"]["max_trades_per_day"] == 2
    assert edited["risk"]["daily_loss_limit"] == -100.0 and engine.journal.path == tmp_path / "journal" / "otro.jsonl"
    # archivo a medio guardar: se muestra el error, se conserva la última configuración buena y la parada sigue disponible
    toml.write_text(TOML + "[auto]\nenabled = \n", encoding="utf-8")
    broken = state()
    assert broken["ok"] is False and broken["config_error"] is True and "risk.toml" in broken["error"]
    assert engine.cfg.auto.max_trades_per_day == 2
    toml.write_text(TOML, encoding="utf-8")
    assert state()["auto"]["enabled"] is False
    # la parada no se enclava: el panel dice si el archivo HALT existe ahora
    (tmp_path / "HALT").write_text("parada puesta a mano\n", encoding="utf-8")
    assert state()["risk"]["halted"] is True
    (tmp_path / "HALT").unlink()
    assert state()["risk"]["halted"] is False


def test_panel_start_explains_a_broken_risk_toml_or_a_missing_terminal_package(fake, tmp_path, monkeypatch, capsys):
    # Como el bucle: un risk.toml mal escrito al arrancar se explica en una línea, sin traza de Python
    toml = tmp_path / "risk.toml"
    toml.write_text(TOML + '[auto]\nenabled = "true"\n', encoding="utf-8")
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", toml)
    monkeypatch.setattr("mt5_mcp.engine.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(dashboard, "MT5Broker", lambda **kw: MT5Broker(module=fake, **kw))
    assert dashboard.main(["--no-open", "--port", "0"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("No se puede abrir el panel; revisa risk.toml: ") and "sin comillas" in out

    def missing(**kw):  # lo que ocurre fuera de Windows o sin el paquete instalado
        raise BrokerError("No se pudo importar el paquete MetaTrader5. Solo existe para Windows x64")

    monkeypatch.setattr(dashboard, "MT5Broker", missing)
    assert dashboard.main(["--no-open", "--port", "0"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("No se puede abrir el panel: No se pudo importar el paquete MetaTrader5") and "--demo" in out


def test_demo_mode_builds_simulated_state():
    engine, on_poll = dashboard._demo_engine()
    monitor = dashboard.Monitor(engine, demo=True, on_poll=on_poll)
    state = monitor.state()
    assert state["ok"] and state["demo"] and len(state["positions"]) == 2 and state["auto"]["enabled"]
    assert {p["opened_by"] for p in state["positions"]} == {"auto", "manual"}
    assert state["auto"]["server_clock_known"] is True and state["clock_settling"] is False
    # la parada del modo demostración va a su carpeta temporal y tampoco se enclava
    assert engine.root != config.PROJECT_ROOT and monitor.halt()["ok"] is True
    assert monitor.state()["risk"]["halted"] is True
    engine.halt_path.unlink()
    monitor._cached = None
    assert monitor.state()["risk"]["halted"] is False


def test_page_describes_every_event_the_engine_records(fake, tmp_path):
    cfg = RiskConfig(symbols=("EURUSD",), max_open_positions=50, max_volume_per_order=1.0, max_total_volume=50.0,
                     auto=AutoConfig(enabled=True, cooldown_minutes=0, one_position_per_symbol=False, max_trades_per_day=50))
    engine = Engine(MT5Broker(module=fake), cfg=cfg, root=tmp_path, journal=Journal(tmp_path / "journal" / "trades.jsonl"), sleep=fake.sleep)
    buy = ("EURUSD", "buy", 1.0981, 1.1041)

    def position(magic):
        return fake.add_position("EURUSD", "buy", 0.1, 1.0990, sl=1.0970, tp=1.1040, magic=magic)

    def every_write():
        mine, bots = position(cfg.magic), position(cfg.auto_magic)
        engine.order_send(*buy, confirm=True)
        engine.auto_order(*buy, WHY)
        engine.modify_position(mine, sl=1.0975, confirm=True)
        engine.auto_tighten_stop(bots, 1.0975, WHY)
        engine.close_position(mine, confirm=True)
        engine.auto_close(bots, WHY)

    engine.order_check(*buy)
    engine.order_send(*buy)  # sin confirm
    engine.order_send("EURUSD", "buy", 1.2000, 1.3000, confirm=True)  # stop al otro lado: lo rechaza el motor de riesgo
    engine.auto_order(*buy, "corto")  # sin motivo
    manual = position(cfg.magic)
    engine.close_position(manual)  # sin confirm
    engine.auto_close(manual, WHY)  # no es del bot
    engine.auto_tighten_stop(manual, 1.0975, WHY)
    every_write()  # el broker lo ejecuta todo
    fake.send_retcode = 10006
    every_write()  # el broker lo rechaza todo
    fake.send_retcode, fake.send_none = 10009, True
    every_write()  # el terminal no devuelve respuesta
    fake.send_none, fake.check_retcode = False, 10013
    engine.order_send(*buy, confirm=True)  # lo rechaza el order_check del broker
    engine.auto_order(*buy, WHY)
    fake.check_retcode, fake.send_retcode = 0, 10008
    placed = [engine.order_send(*buy, confirm=True), engine.auto_order(*buy, WHY)]  # aceptadas por el broker, aún sin ejecutar
    engine.halt_trading("prueba")
    recorded = {e["event"] for e in engine.journal_tail(500)}
    html = dashboard.PAGE.read_text(encoding="utf-8")
    # una orden "placed" se anota como enviada con ese status; la página la distingue de una operación ya abierta
    assert [r["status"] for r in placed] == ["placed", "placed"]
    assert [e["event"] for e in engine.journal_tail(500) if e.get("status") == "placed"] == ["order_sent", "auto_order_sent"]
    assert 'e.status === "placed" && e.event === "auto_order_sent"' in html and 'e.status === "placed" && e.event === "order_sent"' in html
    assert sorted(e for e in recorded if f'case "{e}":' not in html) == []  # sin descripción saldría el nombre interno
    # los intentos y las órdenes sin respuesta del terminal también se han registrado en esta prueba
    assert {"order_attempt", "auto_order_attempt", "order_unknown", "auto_order_unknown", "close_unknown", "auto_close_unknown",
            "modify_unknown", "auto_modify_unknown", "order_sent", "auto_order_sent", "halt"} <= recorded
