"""Servidor MCP (stdio) que expone MetaTrader 5 a Claude Code con límites de riesgo en código."""

from __future__ import annotations

import functools
import sys
from typing import Any, Callable

try:  # mcp >= 2.0 renombró FastMCP a MCPServer
    from mcp.server import MCPServer as _Server
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
- Lee primero account_info y risk_status. Si mode != DEMO y allow_live=false, no se pueden ABRIR posiciones (cerrar y modificar sí).
- Toda orden necesita stop loss. El volumen lo calcula el servidor (position_size / order_check); al enviar, pasa el mismo volume que mostraste al usuario.
- order_send, close_position, modify_position y halt_trading(close_all=true) exigen confirm=true: solo tras confirmación explícita del usuario para esa acción concreta.
- Si una orden falla o se ejecuta parcialmente, no reintentes ni completes el resto por tu cuenta: informa al usuario.
- halt_trading bloquea nuevas órdenes (cerrar y modificar siguen permitidos); reanudar exige que un humano borre el archivo HALT y reinicie el servidor."""

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
        except Exception as exc:  # el SDK ocultaría el mensaje; mejor que el usuario lo vea
            return {"error": f"{type(exc).__name__}: {exc}"}

    return wrapper


@mcp.tool(annotations=READ)
@_safe
def account_info() -> dict:
    """Cuenta conectada: login, modo (DEMO/REAL), balance, equity, divisa, estado de AutoTrading, de la API de Python y del kill switch."""
    return engine().account_info()


@mcp.tool(annotations=READ)
@_safe
def risk_status() -> dict:
    """Estado del motor de riesgo: P&L de hoy, límite diario, posiciones abiertas, límites y bloqueos globales (blockers). El veredicto por orden lo da order_check."""
    return engine().risk_status()


@mcp.tool(annotations=READ)
@_safe
def symbol_info(symbol: str) -> dict:
    """Especificación del símbolo (nombre exacto del broker): dígitos, point, tick value/size, lotes mín/máx/paso, stops level, spread, filling."""
    return engine().symbol_info(symbol)


@mcp.tool(annotations=READ)
@_safe
def quote(symbol: str) -> dict:
    """Último bid/ask, spread en puntos y antigüedad de la cotización (stale=true si tiene más de 5 minutos: mercado probablemente cerrado)."""
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
    """Deals ejecutados hoy (UTC), P&L realizado del día y depósitos/retiradas del día."""
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
    """Evalúa una orden de mercado contra todas las reglas de riesgo y contra el broker (order_check). No envía nada. Usa su summary.volume al enviar."""
    return engine().order_check(symbol, side, sl, tp, volume, comment, risk_pct)


@mcp.tool(annotations=WRITE)
@_safe
def order_send(symbol: str, side: str, sl: float, tp: float | None = None, volume: float | None = None, comment: str = "", confirm: bool = False, risk_pct: float | None = None) -> dict:
    """Envía una orden de MERCADO con SL obligatorio. Requiere confirm=true tras confirmación explícita del usuario y el mismo volume que se le mostró; un status 'partial' o 'placed' no se completa con otra orden."""
    return engine().order_send(symbol, side, sl, tp, volume, comment, confirm, risk_pct)


@mcp.tool(annotations=WRITE)
@_safe
def close_position(ticket: int, confirm: bool = False) -> dict:
    """Cierra a mercado la posición con ese ticket. Requiere confirm=true tras confirmación explícita del usuario. closed=false con status 'partial' significa que queda volumen abierto."""
    return engine().close_position(ticket, confirm)


@mcp.tool(annotations=WRITE)
@_safe
def modify_position(ticket: int, sl: float | None = None, tp: float | None = None, confirm: bool = False) -> dict:
    """Modifica SL/TP de una posición abierta (no se permite quitar el SL; tp=0 quita el TP). Requiere confirm=true tras confirmación explícita del usuario."""
    return engine().modify_position(ticket, sl, tp, confirm)


@mcp.tool(annotations=WRITE)
@_safe
def halt_trading(reason: str = "", close_all: bool = False, confirm: bool = False) -> dict:
    """KILL SWITCH: crea el archivo HALT y bloquea nuevas órdenes hasta que un humano lo borre y reinicie el servidor. close_all=true cierra TODAS las posiciones a mercado y exige confirm=true tras confirmación explícita del usuario."""
    return engine().halt_trading(reason, close_all, confirm)


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
