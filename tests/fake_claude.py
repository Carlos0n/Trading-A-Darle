"""Sustituto de `claude -p` para las pruebas: lee el mensaje por stdin y emite eventos stream-json."""

import json
import os
import sys
import time

mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
prompt = sys.stdin.read()
record = os.environ.get("FAKE_CLAUDE_RECORD")
if record:
    with open(record, "w", encoding="utf-8") as fh:
        json.dump({"argv": sys.argv[1:], "prompt": prompt, "cwd": os.getcwd()}, fh)


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


if mode == "hang":
    emit({"type": "system", "subtype": "init", "mcp_servers": [{"name": "mt5", "status": "connected"}]})
    time.sleep(60)
    sys.exit(0)

status = "failed" if mode == "mcp_failed" else "connected"
emit({"type": "system", "subtype": "init", "mcp_servers": [{"name": "mt5", "status": status}, {"name": "otro", "status": "connected"}]})
if mode == "mcp_failed":
    emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "result": "No tengo herramientas."})
    sys.exit(0)
if mode == "no_tools":
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "Todo en orden, abrí una compra."}]}})
    emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "result": "Todo en orden."})
    sys.exit(0)

emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "Reviso el estado del bot."},
                                                   {"type": "tool_use", "name": "mcp__mt5__auto_status", "input": {}}]}})
emit({"type": "user", "message": {"content": [{"type": "tool_result", "content": [{"type": "text", "text": json.dumps(
    {"enabled": True, "can_open": True, "blockers": [], "trades_today": 0, "limits": {"symbols": ["EURUSD"]}})}]}]}})
emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "mcp__mt5__order_send", "input": {"symbol": "EURUSD"}}]}})
emit({"type": "system", "subtype": "permission_denied", "tool_name": "mcp__mt5__order_send"})
emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "mcp__mt5__auto_order",
                                                   "input": {"symbol": "EURUSD", "side": "buy", "sl": 1.0981, "tp": 1.1041, "reason": "H1 alcista y retroceso"}}]}})
emit({"type": "user", "message": {"content": [{"type": "tool_result", "content": json.dumps({"sent": True, "status": "filled", "retcode_name": "DONE"})}]}})
print("línea que no es JSON", flush=True)
print("aviso por stderr", file=sys.stderr, flush=True)
if mode == "max_turns":
    emit({"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 40})
    sys.exit(1)
emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 4, "total_cost_usd": 0.0312,
      "result": "Abrí una compra en EURUSD.", "permission_denials": [{"tool_name": "mcp__mt5__order_send"}]})
