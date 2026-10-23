"""Pruebas de la frontera MCP: defaults de confirm, anotaciones y manejo de errores del servidor."""

import asyncio
import json

import pytest

from mt5_mcp import server
from mt5_mcp.broker import MT5Broker
from mt5_mcp.config import RiskConfig
from mt5_mcp.engine import Engine
from mt5_mcp.journal import Journal

WRITE_TOOLS = ("order_send", "close_position", "modify_position", "halt_trading")
READ_TOOLS = ("account_info", "risk_status", "symbol_info", "quote", "positions", "pending_orders", "history_today", "journal_tail", "position_size", "order_check")


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
    assert tools == set(WRITE_TOOLS) | set(READ_TOOLS)


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
