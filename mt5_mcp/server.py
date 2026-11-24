"""Servidor MCP (stdio) que expone MetaTrader 5 a Claude Code con límites de riesgo en código."""

from __future__ import annotations

import functools
import sys
import threading
from datetime import datetime, timezone
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
from .config import PROJECT_ROOT, RiskConfig
from .engine import Engine
from .risk import RiskError

INSTRUCTIONS = """Servidor MT5 con motor de riesgo. Reglas:
- Lee primero account_info y risk_status. Si mode != DEMO y allow_live=false, no se pueden ABRIR posiciones (cerrar y modificar sí).
- La primera llamada, o una tras un par de minutos sin llamadas, puede tardar unos segundos (hasta 6 con el mercado cerrado): el servidor espera a ver moverse una cotización para conocer la hora del broker. Con el bloqueo "hora del servidor desconocida" no se abre nada (cerrar y modificar sí): explica el motivo al usuario y no insistas.
- Toda orden necesita stop loss. El volumen lo calcula el servidor (position_size / order_check); al enviar, pasa el mismo volume que mostraste al usuario.
- order_send, close_position, modify_position y halt_trading(close_all=true) exigen confirm=true: solo tras confirmación explícita del usuario para esa acción concreta.
- Si una orden falla, se ejecuta parcialmente o devuelve status "unknown" (el terminal no confirmó el resultado), no reintentes ni completes el resto por tu cuenta: comprueba positions e informa al usuario.
- journal_error en una respuesta con sent, closed o modified en true no es un fallo de la orden: la operación se hizo y solo falló su anotación en el journal. Díselo al usuario.
- halt_trading bloquea nuevas órdenes (cerrar y modificar siguen permitidos); reanudar exige que un humano borre el archivo HALT y reinicie el servidor.
- Los números de mercado (velas, EMA, RSI, ATR) salen de la herramienta bars; no los estimes de memoria.
- auto_order, auto_close y auto_tighten_stop operan SIN confirmación y solo funcionan si el usuario activó el modo automático en su configuración. Úsalas únicamente dentro de un ciclo automático que el usuario haya lanzado (bucle mt5_mcp.autoloop, protocolo auto_protocol.md), nunca para saltarte la confirmación en una conversación normal."""

mcp = _Server("mt5", instructions=INSTRUCTIONS)
_engine: Engine | None = None
# Motivo de una parada pedida cuando el engine no se pudo construir (risk.toml inválido, falta el paquete MetaTrader5).
_halt_pending: str | None = None

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True) if ToolAnnotations else None
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True) if ToolAnnotations else None


def engine() -> Engine:
    global _engine, _halt_pending
    if _engine is None:
        eng = Engine(MT5Broker())
        if _halt_pending is not None:
            # Se pidió parar cuando este servidor aún no podía iniciarse: el engine nace parado, con el archivo de
            # parada de su configuración (que puede no llamarse como el de por defecto) y la parada anotada en el journal.
            eng.halt_trading(_halt_pending)
            _halt_pending = None
        _engine = eng
    return _engine


def _halt_without_engine(reason: str, problem: Exception) -> dict:
    """Parada cuando el engine no se puede construir: sin configuración ni terminal, pero el kill switch no depende de ellos.

    Crea el archivo de parada con el nombre por defecto, no cierra nada y deja la parada pendiente para aplicarla al
    engine en cuanto se pueda construir (así sigue parado aunque el usuario corrija risk.toml sin reiniciar el servidor).
    """
    global _halt_pending
    reason = reason or "halt solicitado"
    _halt_pending = reason
    path = PROJECT_ROOT / RiskConfig().halt_file
    errors = [
        f"el servidor no pudo iniciarse ({problem}): no se cerró ninguna posición y el archivo de parada se creó con el "
        f"nombre por defecto ({path.name}). Si hay posiciones abiertas, ciérralas en el terminal MT5"
    ]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {reason} (servidor sin iniciar)\n")
    except OSError as exc:
        if not path.exists():
            errors.append(f"no se pudo crear el archivo {path} ({exc}): créalo a mano para detener también el bucle automático")
    return {
        "halted": True,
        "halt_file": str(path),
        "closed": [],
        "errors": errors,
        "resume": f"para reanudar, un humano debe borrar el archivo {path} y reiniciar el servidor MCP",
    }


# El SDK ejecuta cada herramienta en un hilo propio. La API de MetaTrader5 y el estado del engine no admiten
# llamadas simultáneas, y dos órdenes a la vez podrían saltarse los cupos: las herramientas van de una en una.
_LOCK = threading.RLock()
# La parada no hace cola detrás de una lectura colgada: pasados estos segundos sigue adelante sin el candado.
HALT_LOCK_WAIT = 2.0


def _safe(fn: Callable[..., Any] | None = None, *, urgent: bool = False) -> Any:
    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            held = _LOCK.acquire(timeout=HALT_LOCK_WAIT) if urgent else _LOCK.acquire()
            try:
                return func(*args, **kwargs)
            except (BrokerError, RiskError, ValueError) as exc:
                return {"error": str(exc)}
            except Exception as exc:  # el SDK ocultaría el mensaje; mejor que el usuario lo vea
                return {"error": f"{type(exc).__name__}: {exc}"}
            finally:
                if held:
                    _LOCK.release()

        return wrapper

    return decorate(fn) if fn is not None else decorate


@mcp.tool(annotations=READ)
@_safe
def account_info() -> dict:
    """Cuenta conectada: login, modo (DEMO/REAL), balance, equity, divisa, estado de AutoTrading, de la API de Python y del kill switch."""
    return engine().account_info()


@mcp.tool(annotations=READ)
@_safe
def risk_status() -> dict:
    """Estado del motor de riesgo: P&L de hoy, límite diario, posiciones abiertas, límites y bloqueos globales (blockers). El veredicto por orden lo da order_check. day_window dice qué día cuentan los importes de hoy: con server_clock_known=false esa ventana no está verificada y no se abren posiciones (blockers trae el motivo)."""
    return engine().risk_status()


@mcp.tool(annotations=READ)
@_safe
def symbol_info(symbol: str) -> dict:
    """Especificación del símbolo (nombre exacto del broker): dígitos, point, tick value/size, lotes mín/máx/paso, stops level, spread, filling."""
    return engine().symbol_info(symbol)


@mcp.tool(annotations=READ)
@_safe
def quote(symbol: str) -> dict:
    """Último bid/ask, spread en puntos y antigüedad de la cotización. stale=true si tiene más de 5 minutos (mercado probablemente cerrado) o si aún no se conoce la hora del servidor (age_seconds=null)."""
    return engine().quote(symbol)


@mcp.tool(annotations=READ)
@_safe
def bars(symbol: str, timeframe: str = "M15", count: int = 30) -> dict:
    """Velas cerradas (M1, M5, M15, M30, H1, H4, D1) con indicadores calculados por el servidor: EMA20, EMA50, RSI14, ATR14, tendencia, máximo y mínimo de 20 velas. count: velas a devolver (5 a 200). forming_bar es null cuando la última vela ya cerró (mercado parado o vela nueva aún sin ticks). Si forming_bar_may_be_closed=true no se puede asegurar que forming_bar siga abierta (hora del servidor desconocida o ninguna cotización desde antes de su cierre): last_closed_bar y los indicadores pueden ir una vela por detrás, no decidas con ellos."""
    return engine().bars(symbol, timeframe, count)


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
    """Deals ejecutados hoy (día UTC), P&L realizado del día (operaciones y cargos del broker) y depósitos/retiradas del día. day_window dice si esa ventana está verificada: con server_clock_known=false no lo está y los importes pueden incluir operaciones de ayer o dejar fuera alguna de hoy."""
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
    """Envía una orden de MERCADO con SL obligatorio. Requiere confirm=true tras confirmación explícita del usuario y el mismo volume que se le mostró; un status 'partial' o 'placed' no se completa con otra orden. Con status 'unknown' el terminal no confirmó el resultado y la orden pudo llegar al broker: comprueba positions y no la repitas. journal_error con sent=true no es un fallo de la orden."""
    return engine().order_send(symbol, side, sl, tp, volume, comment, confirm, risk_pct)


@mcp.tool(annotations=WRITE)
@_safe
def close_position(ticket: int, confirm: bool = False) -> dict:
    """Cierra a mercado la posición con ese ticket. Requiere confirm=true tras confirmación explícita del usuario. closed=false con status 'partial' significa que queda volumen abierto; con status 'unknown' el terminal no confirmó el resultado: comprueba positions y no lo repitas. journal_error con closed=true no es un fallo del cierre."""
    return engine().close_position(ticket, confirm)


@mcp.tool(annotations=WRITE)
@_safe
def modify_position(ticket: int, sl: float | None = None, tp: float | None = None, confirm: bool = False) -> dict:
    """Modifica SL/TP de una posición abierta (no se permite quitar el SL; tp=0 quita el TP). Requiere confirm=true tras confirmación explícita del usuario. Con status 'unknown' el terminal no confirmó el cambio: comprueba positions y no lo repitas. journal_error con modified=true no es un fallo del cambio."""
    return engine().modify_position(ticket, sl, tp, confirm)


@mcp.tool(annotations=WRITE)
@_safe(urgent=True)
def halt_trading(reason: str = "", close_all: bool = False, confirm: bool = False) -> dict:
    """KILL SWITCH: crea el archivo HALT y bloquea nuevas órdenes hasta que un humano lo borre y reinicie el servidor. close_all=true cierra TODAS las posiciones a mercado y exige confirm=true tras confirmación explícita del usuario. errors lista lo que no se pudo hacer (posiciones sin cerrar, archivo sin crear). Si el servidor no puede iniciarse (risk.toml inválido o falta el paquete MetaTrader5) crea igualmente HALT, no cierra ninguna posición y lo dice en errors."""
    try:
        eng = engine()
    except Exception as exc:  # la parada no depende de que el servidor haya podido iniciarse
        return _halt_without_engine(reason, exc)
    return eng.halt_trading(reason, close_all, confirm)


@mcp.tool(annotations=READ)
@_safe
def auto_status() -> dict:
    """Estado del modo automático: si está activado, bloqueos, operaciones de hoy, racha de pérdidas, enfriamiento, posiciones del bot y límites. can_open=false también cuando no se conoce la hora del servidor (cooldown_remaining_minutes=null) o cuando ningún símbolo de la lista cotiza; can_manage_positions dice si el bot puede seguir gestionando sus posiciones."""
    return engine().auto_status()


@mcp.tool(annotations=WRITE)
@_safe
def auto_order(symbol: str, side: str, sl: float, tp: float, reason: str) -> dict:
    """MODO AUTOMÁTICO: orden de mercado sin confirmación humana, con SL y TP obligatorios. El servidor fija el volumen por riesgo y la rechaza si el modo automático no está activado o si se incumple cualquier límite. reason: regla de la estrategia que se cumple. Con status 'unknown' el terminal no confirmó el resultado: comprueba positions y no la repitas. journal_error con sent=true no es un fallo de la orden."""
    return engine().auto_order(symbol, side, sl, tp, reason)


@mcp.tool(annotations=WRITE)
@_safe
def auto_close(ticket: int, reason: str) -> dict:
    """MODO AUTOMÁTICO: cierra a mercado una posición que abrió el propio bot (nunca una manual). reason: regla de salida que se cumple. Con status 'unknown' el terminal no confirmó el resultado: comprueba positions y no lo repitas. journal_error con closed=true no es un fallo del cierre."""
    return engine().auto_close(ticket, reason)


@mcp.tool(annotations=WRITE)
@_safe
def auto_tighten_stop(ticket: int, sl: float, reason: str) -> dict:
    """MODO AUTOMÁTICO: mueve el stop loss de una posición del bot solo en la dirección que reduce el riesgo (nunca lo aleja ni lo quita). Con status 'unknown' el terminal no confirmó el cambio: comprueba positions y no lo repitas. journal_error con modified=true no es un fallo del cambio."""
    return engine().auto_tighten_stop(ticket, sl, reason)


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
