"""Pruebas de la frontera MCP: defaults de confirm, anotaciones y manejo de errores del servidor."""

import asyncio
import json
import threading

import pytest

from mt5_mcp import server
from mt5_mcp.broker import MT5Broker
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
    eng = Engine(MT5Broker(module=fake), cfg=RiskConfig(symbols=("EURUSD",), max_volume_per_order=0.5, max_total_volume=1.0), root=tmp_path, journal=Journal(tmp_path / "j.jsonl"))
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
