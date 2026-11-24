"""Pruebas de la frontera MCP: defaults de confirm, anotaciones y manejo de errores del servidor."""

import asyncio
import json
import threading

import pytest

from mt5_mcp import config, server
from mt5_mcp.broker import BrokerError, MT5Broker
from mt5_mcp.config import RiskConfig
from mt5_mcp.engine import Engine
from mt5_mcp.journal import Journal

WRITE_TOOLS = ("order_send", "close_position", "modify_position", "halt_trading")
AUTO_WRITE_TOOLS = ("auto_order", "auto_close", "auto_tighten_stop")
READ_TOOLS = ("account_info", "risk_status", "symbol_info", "quote", "bars", "positions", "pending_orders", "history_today", "journal_tail", "position_size", "order_check", "auto_status")


def _schema(tool):
    return getattr(tool, "input_schema", None) or getattr(tool, "inputSchema")


def _ann(tool, snake, camel):
    ann = tool.annotations
    return getattr(ann, snake, None) if hasattr(ann, snake) else getattr(ann, camel)


def _call(name, args):
    res = asyncio.run(server.mcp.call_tool(name, args))
    content = getattr(res, "content", res)
    if isinstance(content, tuple):
        content = content[0]
    text = content[0].text
    return json.loads(text)


@pytest.fixture
def served(fake, tmp_path, monkeypatch):
    eng = Engine(MT5Broker(module=fake), cfg=RiskConfig(symbols=("EURUSD",), max_volume_per_order=0.5, max_total_volume=1.0), root=tmp_path, journal=Journal(tmp_path / "j.jsonl"), sleep=fake.sleep)
    monkeypatch.setattr(server, "_engine", eng)
    return eng


def test_all_tools_registered():
    tools = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert tools == set(WRITE_TOOLS) | set(AUTO_WRITE_TOOLS) | set(READ_TOOLS)


def test_write_tools_default_confirm_false_and_are_marked_destructive():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    for name in WRITE_TOOLS:
        props = _schema(tools[name])["properties"]
        assert "confirm" in props and props["confirm"].get("default") is False, name
        assert "confirm" not in _schema(tools[name]).get("required", []), name
        assert _ann(tools[name], "destructive_hint", "destructiveHint") is True, name
        assert _ann(tools[name], "read_only_hint", "readOnlyHint") is False, name
    for name in READ_TOOLS:
        assert _ann(tools[name], "read_only_hint", "readOnlyHint") is True, name
        assert "confirm" not in _schema(tools[name])["properties"], name
    for name in AUTO_WRITE_TOOLS:
        schema = _schema(tools[name])
        assert _ann(tools[name], "destructive_hint", "destructiveHint") is True, name
        assert _ann(tools[name], "read_only_hint", "readOnlyHint") is False, name
        assert "reason" in schema.get("required", []), name
        assert "volume" not in schema["properties"] and "risk_pct" not in schema["properties"], name
    assert {"sl", "tp"} <= set(_schema(tools["auto_order"]).get("required", []))


def test_auto_tools_are_inert_when_auto_mode_is_off(served, fake):
    out = _call("auto_order", {"symbol": "EURUSD", "side": "buy", "sl": 1.0981, "tp": 1.1041, "reason": "tendencia alcista y retroceso a la EMA20"})
    assert out["sent"] is False and any("modo automático desactivado" in r for r in out["reasons"]) and not fake.sent
    t = fake.add_position("EURUSD", "buy", 0.1, 1.0990, sl=1.0970, magic=served.cfg.auto_magic)
    assert _call("auto_close", {"ticket": t, "reason": "salida por regla de la estrategia"})["closed"] is False
    assert _call("auto_tighten_stop", {"ticket": t, "sl": 1.0985, "reason": "proteger tras avanzar 1R"})["modified"] is False
    assert not fake.sent
    assert _call("auto_status", {})["enabled"] is False


def test_order_send_through_mcp_requires_confirm(served, fake):
    out = _call("order_send", {"symbol": "EURUSD", "side": "buy", "sl": 1.0981})
    assert out["sent"] is False and out["status"] == "needs_confirmation" and not fake.sent
    out2 = _call("order_send", {"symbol": "EURUSD", "side": "buy", "sl": 1.0981, "confirm": True})
    assert out2["sent"] is True and fake.sent


def test_errors_are_returned_as_text_not_hidden(served, fake):
    assert "no existe" in _call("symbol_info", {"symbol": "XXX"})["error"]
    assert "side inválido" in _call("order_send", {"symbol": "EURUSD", "side": "hold", "sl": 1.0981, "confirm": True})["error"]
    fake.fail_positions = True
    out = _call("order_send", {"symbol": "EURUSD", "side": "buy", "sl": 1.0981, "confirm": True})
    assert "positions_get" in out["error"] and not fake.sent


def test_unexpected_exception_is_reported(served, fake, monkeypatch):
    monkeypatch.setattr(served, "account_info", lambda: (_ for _ in ()).throw(KeyError("boom")))
    out = _call("account_info", {})
    assert out["error"].startswith("KeyError")


def test_tools_run_one_at_a_time(served, monkeypatch):
    # El SDK lanza cada herramienta en un hilo: sin candado, dos lecturas pisarían la API de MT5 y el estado del engine
    inside, release, order = threading.Event(), threading.Event(), []

    def slow_account():
        order.append("account:in")
        inside.set()
        release.wait(5)
        order.append("account:out")
        return {"ok": 1}

    monkeypatch.setattr(served, "account_info", slow_account)
    monkeypatch.setattr(served, "risk_status", lambda: order.append("risk") or {"ok": 2})
    first = threading.Thread(target=server.account_info)
    first.start()
    assert inside.wait(5)
    second = threading.Thread(target=server.risk_status)
    second.start()
    second.join(0.2)
    assert second.is_alive() and order == ["account:in"]  # la segunda espera a la primera
    release.set()
    first.join(5)
    second.join(5)
    assert order == ["account:in", "account:out", "risk"]


def test_halt_does_not_queue_behind_a_stuck_read(served, monkeypatch):
    monkeypatch.setattr(server, "HALT_LOCK_WAIT", 0.05)
    inside, release = threading.Event(), threading.Event()
    monkeypatch.setattr(served, "account_info", lambda: (inside.set(), release.wait(5), {"ok": 1})[2])
    stuck = threading.Thread(target=server.account_info)
    stuck.start()
    assert inside.wait(5)
    try:
        out = server.halt_trading("terminal colgado")
        assert out["halted"] is True and served.halt_path.exists()
    finally:
        release.set()
        stuck.join(5)
    # el candado quedó libre: la siguiente herramienta responde
    assert "error" not in server.risk_status()


# ---- parada cuando el servidor no puede iniciarse -------------------------------------------------------------

BAD_TOML = 'symbols = ["EURUSD"]\n[auto]\nenabled = "true"\n'  # el interruptor entre comillas: risk.toml no carga


@pytest.fixture
def unserved(fake, tmp_path, monkeypatch):
    """Servidor sin engine, con la raíz del proyecto y risk.toml en una carpeta temporal (nunca los del repositorio).

    El terminal es el doble; `unserved.terminal = None` simula que falta el paquete MetaTrader5.
    """
    class Setup:
        root = tmp_path
        toml = tmp_path / "risk.toml"
        terminal = fake

    def broker():
        if Setup.terminal is None:
            raise BrokerError("No se pudo importar el paquete MetaTrader5. Solo existe para Windows x64")
        return MT5Broker(module=Setup.terminal)

    monkeypatch.setattr(server, "_engine", None)
    monkeypatch.setattr(server, "_halt_pending", None)
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", Setup.toml)
    monkeypatch.setattr("mt5_mcp.engine.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(server, "MT5Broker", broker)
    monkeypatch.setattr(server, "Engine", lambda b: Engine(b, sleep=fake.sleep))  # la espera del reloj no duerme de verdad
    return Setup


def test_halt_with_an_invalid_risk_toml_still_creates_the_halt_file(unserved, fake):
    unserved.toml.write_text(BAD_TOML, encoding="utf-8")
    fake.add_position("EURUSD", "buy", 0.1, 1.0990, sl=1.0950)
    assert "sin comillas" in _call("risk_status", {})["error"]  # el servidor no puede iniciarse: las demás herramientas lo dicen
    out = _call("halt_trading", {"reason": "pánico"})
    halt = unserved.root / "HALT"
    assert out["halted"] is True and "error" not in out and out["closed"] == []
    assert out["halt_file"] == str(halt) and halt.read_text(encoding="utf-8").strip().endswith("pánico (servidor sin iniciar)")
    assert len(out["errors"]) == 1 and "el servidor no pudo iniciarse" in out["errors"][0]
    assert "sin comillas" in out["errors"][0] and "no se cerró ninguna posición" in out["errors"][0]
    assert str(halt) in out["resume"]
    # con close_all y confirm tampoco se cierra nada: no hay configuración con la que operar, y se dice
    again = _call("halt_trading", {"reason": "y cierra", "close_all": True, "confirm": True})
    assert again["halted"] is True and again["closed"] == [] and "no se cerró ninguna posición" in again["errors"][0]
    assert not fake.sent and len(fake.positions_get()) == 1
    assert len(halt.read_text(encoding="utf-8").splitlines()) == 2
    assert sorted(p.name for p in unserved.root.iterdir()) == ["HALT", "risk.toml"]  # ni journal ni nada más


def test_halt_without_the_metatrader5_package_still_creates_the_halt_file(unserved):
    unserved.terminal = None  # risk.toml válido (no hay archivo: valores por defecto), pero no hay paquete MetaTrader5
    out = server.halt_trading()
    halt = unserved.root / "HALT"
    assert out["halted"] is True and out["halt_file"] == str(halt) and out["closed"] == []
    assert halt.read_text(encoding="utf-8").strip().endswith("halt solicitado (servidor sin iniciar)")
    assert "el servidor no pudo iniciarse" in out["errors"][0] and "paquete MetaTrader5" in out["errors"][0]


def test_halt_asked_before_the_server_could_start_holds_once_risk_toml_is_fixed(unserved, fake):
    unserved.toml.write_text(BAD_TOML, encoding="utf-8")
    assert server.halt_trading("pánico")["halted"] is True
    # el usuario corrige risk.toml sin reiniciar el servidor, y su archivo de parada no se llama como el de por defecto
    unserved.toml.write_text('symbols = ["EURUSD"]\nhalt_file = "PARADA"\n', encoding="utf-8")
    status = _call("risk_status", {})
    assert status["halted"] is True and status["can_open_new_positions"] is False and any("kill switch" in b for b in status["blockers"])
    assert (unserved.root / "PARADA").read_text(encoding="utf-8").strip().endswith("pánico")
    out = _call("order_send", {"symbol": "EURUSD", "side": "buy", "sl": 1.0981, "confirm": True})
    assert out["sent"] is False and not fake.sent
    halts = [e for e in server.journal_tail(20) if e["event"] == "halt"]  # lista: _call solo lee respuestas de un objeto
    assert len(halts) == 1 and halts[0]["reason"] == "pánico"  # la parada queda anotada en cuanto hay journal
    assert server._halt_pending is None and _call("account_info", {})["halted"] is True


def test_halt_without_engine_reports_a_halt_file_it_could_not_create(unserved):
    unserved.toml.write_text(BAD_TOML, encoding="utf-8")
    (unserved.root / "HALT").mkdir()  # una carpeta con ese nombre: el archivo existe, la parada vale
    assert server.halt_trading("x")["errors"][0].startswith("el servidor no pudo iniciarse")
    assert len(server.halt_trading("x")["errors"]) == 1


def test_halt_without_engine_does_not_queue_behind_a_stuck_tool(unserved, monkeypatch):
    unserved.toml.write_text(BAD_TOML, encoding="utf-8")
    monkeypatch.setattr(server, "HALT_LOCK_WAIT", 0.05)
    held, release = threading.Event(), threading.Event()

    def hold():
        with server._LOCK:
            held.set()
            release.wait(5)

    stuck = threading.Thread(target=hold)
    stuck.start()
    assert held.wait(5)
    try:
        out = server.halt_trading("terminal colgado")
        assert out["halted"] is True and (unserved.root / "HALT").exists()
    finally:
        release.set()
        stuck.join(5)
    assert "sin comillas" in server.risk_status()["error"]  # el candado quedó libre


def test_halt_with_a_working_server_is_unchanged(served, fake):
    t = fake.add_position("EURUSD", "buy", 0.1, 1.0990, sl=1.0950)
    out = _call("halt_trading", {"reason": "fin", "close_all": True, "confirm": True})
    assert out["halted"] is True and out["errors"] == [] and [c["ticket"] for c in out["closed"]] == [t]
    assert served.halt_path.exists() and server._halt_pending is None


# ---- lo que dicen las descripciones existe de verdad ----------------------------------------------------------

# Claves que nombra la descripción de cada herramienta de lectura: tienen que estar en lo que devuelve.
DOCUMENTED_KEYS = {
    "risk_status": ({}, ("blockers", "day_window", "server_clock_known")),
    "quote": ({"symbol": "EURUSD"}, ("stale", "age_seconds")),
    "bars": ({"symbol": "EURUSD"}, ("forming_bar", "forming_bar_may_be_closed", "last_closed_bar")),
    "history_today": ({}, ("day_window", "server_clock_known")),
    "auto_status": ({}, ("can_open", "can_manage_positions", "cooldown_remaining_minutes")),
}


def test_keys_named_in_the_descriptions_exist_in_the_results(served, fake):
    fake.set_rates("EURUSD", fake.TIMEFRAME_M15, [1.1 + i * 0.0001 for i in range(80)])
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    for name, (args, keys) in DOCUMENTED_KEYS.items():
        out = _call(name, args)
        for key in keys:
            assert key in tools[name].description, (name, key)
            assert key in out, (name, key, out.get("error"))


def test_descriptions_explain_an_unknown_server_clock(served, fake):
    # mercado cerrado: lo que las descripciones dicen de la hora desconocida es lo que pasa
    fake.set_rates("EURUSD", fake.TIMEFRAME_M15, [1.1 + i * 0.0001 for i in range(80)])
    fake.freeze_ticks(age=3600)
    q = _call("quote", {"symbol": "EURUSD"})
    assert q["stale"] is True and q["age_seconds"] is None
    status = _call("risk_status", {})
    assert status["server_clock_known"] is False and status["can_open_new_positions"] is False
    assert any(b.startswith("hora del servidor desconocida") for b in status["blockers"]) and "SIN VERIFICAR" in status["day_window"]
    assert _call("history_today", {})["server_clock_known"] is False
    auto = _call("auto_status", {})
    assert auto["can_open"] is False and auto["cooldown_remaining_minutes"] is None
    assert "hora del servidor desconocida" in server.INSTRUCTIONS and "hasta 6" in server.INSTRUCTIONS


def test_write_tools_explain_unknown_results_and_journal_errors():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    for name, flag in (("order_send", "sent"), ("auto_order", "sent"), ("close_position", "closed"), ("auto_close", "closed"),
                       ("modify_position", "modified"), ("auto_tighten_stop", "modified")):
        text = tools[name].description
        assert "status 'unknown'" in text and "comprueba positions" in text, name
        assert f"journal_error con {flag}=true no es un fallo" in text, name
    assert "errors" in tools["halt_trading"].description and "risk.toml inválido" in tools["halt_trading"].description
    assert "journal_error" in server.INSTRUCTIONS and 'status "unknown"' in server.INSTRUCTIONS


def test_instructions_keep_the_automatic_tools_out_of_normal_conversations():
    text = server.INSTRUCTIONS
    assert "auto_order, auto_close y auto_tighten_stop operan SIN confirmación" in text
    assert "Úsalas únicamente dentro de un ciclo automático que el usuario haya lanzado" in text
    assert "nunca para saltarte la confirmación en una conversación normal" in text
    assert "exigen confirm=true: solo tras confirmación explícita del usuario" in text


def test_halt_without_engine_uses_the_halt_file_declared_in_risk_toml(unserved, fake):
    # risk.toml declara halt_file = "PARADA" y tiene otro error: el bucle y otros servidores miran PARADA
    unserved.toml.write_text('symbols = ["EURUSD"]\nhalt_file = "PARADA"\n[auto]\nenabled = "true"\n', encoding="utf-8")
    out = server.halt_trading("pánico")
    assert out["halted"] is True and out["halt_file"] == str(unserved.root / "PARADA")
    assert (unserved.root / "PARADA").exists() and not (unserved.root / "HALT").exists()
    assert "el archivo de parada PARADA se creó" in out["errors"][0] and len(out["errors"]) == 1


def test_halt_without_engine_does_not_claim_a_file_it_could_not_create(unserved):
    unserved.toml.write_text(BAD_TOML, encoding="utf-8")
    (unserved.root / "HALT").symlink_to(unserved.root / "no-existe" / "x")  # open() falla y el archivo no existe
    out = server.halt_trading("pánico")
    assert out["halted"] is True and len(out["errors"]) == 2
    assert "el archivo de parada HALT NO se pudo crear" in out["errors"][0] and "se creó" not in out["errors"][0]
    assert out["errors"][1].startswith(f"no se pudo crear el archivo {unserved.root / 'HALT'}")
