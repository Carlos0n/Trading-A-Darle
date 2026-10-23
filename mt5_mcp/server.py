"""Servidor MCP (stdio) que expone MetaTrader 5 a Claude Code con límites de riesgo en código."""

from __future__ import annotations

import functools
import sys
from typing import Any, Callable

try:  # mcp >= 2.0 renombró FastMCP a MCPServer
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]

try:
    from mcp.types import ToolAnnotations
except ImportError:  # pragma: no cover - versiones antiguas del SDK
    ToolAnnotations = None  # type: ignore[assignment]

from .broker import BrokerError, MT5Broker
from .engine import Engine
from .risk import RiskError

INSTRUCTIONS = """Servidor MT5 con motor de riesgo. Reglas:
- Lee primero account_info y risk_status. Si mode != DEMO y allow_live=false, no se puede operar.
- Toda orden necesita stop loss. El volumen lo calcula el servidor (position_size / order_check).
- order_send, close_position y modify_position exigen confirm=true: solo tras confirmación explícita del usuario.
- Si una orden falla, no reintentes en bucle: informa al usuario.
- halt_trading detiene todo; reanudar exige que un humano borre el archivo HALT."""

mcp = _Server("mt5", instructions=INSTRUCTIONS)
_engine: Engine | None = None

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True) if ToolAnnotations else None
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True) if ToolAnnotations else None


def engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = Engine(MT5Broker())
    return _engine


def _safe(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except (BrokerError, RiskError, ValueError) as exc:
            return {"error": str(exc)}

    return wrapper


@mcp.tool(annotations=READ)
@_safe
def account_info() -> dict:
    """Cuenta conectada: login, modo (DEMO/REAL), balance, equity, divisa, estado de AutoTrading y del kill switch."""
    return engine().account_info()


@mcp.tool(annotations=READ)
@_safe
def risk_status() -> dict:
    """Estado del motor de riesgo: P&L de hoy, límite diario, posiciones abiertas, límites configurados y si se puede abrir."""
    return engine().risk_status()


@mcp.tool(annotations=READ)
@_safe
def symbol_info(symbol: str) -> dict:
    """Especificación del símbolo: dígitos, point, tick value/size, lotes mín/máx/paso, stops level, spread, filling."""
    return engine().symbol_info(symbol)


@mcp.tool(annotations=READ)
@_safe
def quote(symbol: str) -> dict:
    """Último bid/ask y spread en puntos de un símbolo."""
    return engine().quote(symbol)


@mcp.tool(annotations=READ)
@_safe
def positions(symbol: str | None = None) -> list[dict]:
    """Posiciones abiertas (opcionalmente filtradas por símbolo) con ticket, lado, volumen, SL/TP y P&L flotante."""
    return engine().positions(symbol)


@mcp.tool(annotations=READ)
@_safe
def pending_orders() -> list[dict]:
    """Órdenes pendientes en el terminal."""
    return engine().pending_orders()


@mcp.tool(annotations=READ)
@_safe
def history_today() -> dict:
    """Deals ejecutados hoy (UTC) y P&L realizado del día."""
    return engine().history_today()


@mcp.tool(annotations=READ)
@_safe
def journal_tail(n: int = 20) -> list[dict]:
    """Últimas entradas del journal de operaciones (journal/trades.jsonl)."""
    return engine().journal_tail(n)


@mcp.tool(annotations=READ)
@_safe
def position_size(symbol: str, side: str, sl: float, risk_pct: float | None = None) -> dict:
    """Calcula el volumen máximo permitido para arriesgar risk_pct (por defecto el máximo configurado) con ese SL. No envía nada."""
    return engine().position_size(symbol, side, sl, risk_pct)


@mcp.tool(annotations=READ)
@_safe
def order_check(symbol: str, side: str, sl: float, tp: float | None = None, volume: float | None = None, comment: str = "", risk_pct: float | None = None) -> dict:
    """Evalúa una orden de mercado contra todas las reglas de riesgo y contra el broker (order_check). No envía nada."""
    return engine().order_check(symbol, side, sl, tp, volume, comment, risk_pct)


@mcp.tool(annotations=WRITE)
@_safe
def order_send(symbol: str, side: str, sl: float, tp: float | None = None, volume: float | None = None, comment: str = "", confirm: bool = False, risk_pct: float | None = None) -> dict:
    """Envía una orden de MERCADO con SL obligatorio. Requiere confirm=true tras confirmación explícita del usuario; sin volumen, usa el calculado por riesgo."""
    return engine().order_send(symbol, side, sl, tp, volume, comment, confirm, risk_pct)


@mcp.tool(annotations=WRITE)
@_safe
def close_position(ticket: int, confirm: bool = False) -> dict:
    """Cierra a mercado la posición con ese ticket. Requiere confirm=true tras confirmación explícita del usuario."""
    return engine().close_position(ticket, confirm)


@mcp.tool(annotations=WRITE)
@_safe
def modify_position(ticket: int, sl: float | None = None, tp: float | None = None, confirm: bool = False) -> dict:
    """Modifica SL/TP de una posición abierta (no se permite quitar el SL). Requiere confirm=true tras confirmación explícita del usuario."""
    return engine().modify_position(ticket, sl, tp, confirm)


@mcp.tool(annotations=WRITE)
@_safe
def halt_trading(reason: str = "", close_all: bool = False) -> dict:
    """KILL SWITCH: crea el archivo HALT (bloquea nuevas órdenes) y, si close_all=true, cierra todas las posiciones. Solo un humano puede reanudar borrando el archivo."""
    return engine().halt_trading(reason, close_all)


def main() -> None:
    try:
        mcp.run()
    finally:
        if _engine is not None:
            try:
                _engine.b.shutdown()
            except Exception:  # pragma: no cover
                pass


if __name__ == "__main__":
    sys.exit(main())
