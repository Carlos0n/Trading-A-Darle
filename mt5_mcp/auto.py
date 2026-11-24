"""Reglas del modo automático: lógica pura sobre el histórico de operaciones y las posiciones abiertas."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .config import RiskConfig
from .risk import AccountSnapshot

DEAL_ENTRY_IN = 0
DEAL_ENTRY_OUT = 1
DEAL_ENTRY_INOUT = 2
DEAL_ENTRY_OUT_BY = 3

ENABLE_HINT = (
    "modo automático desactivado. Solo el usuario puede activarlo: añade al FINAL del archivo de configuración de riesgo "
    "las dos líneas `[auto]` y `enabled = true`, y reinicia el servidor MCP (/mcp)"
)


def deal_net(deal: dict) -> float:
    return deal["profit"] + deal["commission"] + deal["swap"] + deal["fee"]


@dataclass(frozen=True)
class AutoStats:
    trades_today: int = 0
    last_entry_time: int = 0  # tiempo del servidor (segundos) de la última entrada automática, sea de hoy o anterior
    consecutive_losses: int = 0
    closed_today: int = 0
    wins_today: int = 0
    realized_today: float = 0.0
    open_positions: tuple[dict, ...] = field(default_factory=tuple)

    @property
    def open_symbols(self) -> tuple[str, ...]:
        return tuple(p["symbol"] for p in self.open_positions)


def is_bot_position(position: dict, auto_magic: int, known_ids: frozenset[int] = frozenset()) -> bool:
    """La posición es del bot si lleva su magic o si el bot la anotó al abrirla (hay brokers que borran el magic)."""
    return position.get("magic") == auto_magic or position.get("identifier", position.get("ticket")) in known_ids


def compute_stats(deals: list[dict], positions: list[dict], auto_magic: int, day_start: int,
                  known_ids: frozenset[int] = frozenset()) -> AutoStats:
    """Estadísticas del bot a partir de los deals (ventana que incluye hoy y días anteriores) y posiciones abiertas.

    Una posición es "del bot" si su primer deal de entrada lleva el magic automático o si su identificador está
    en `known_ids` (el registro que el bot escribe al abrir). Se agrupa por position_id porque el deal de salida
    generado por SL/TP no siempre conserva el magic, y algunos brokers lo borran también de la entrada.
    """
    by_pos: dict[int, list[dict]] = {}
    for d in sorted(deals, key=lambda x: (x["time"], x["ticket"])):
        if d["position_id"]:
            by_pos.setdefault(d["position_id"], []).append(d)

    open_ids = {p.get("identifier", p["ticket"]) for p in positions}
    auto_open = tuple(p for p in positions if is_bot_position(p, auto_magic, known_ids))

    entries_today: list[int] = []
    last_entry = 0  # sobre toda la ventana: el enfriamiento no se reinicia al cambiar el día
    closed: list[tuple[int, int, float]] = []  # (hora del último cierre, ticket de ese deal, neto)
    for pos_id, group in by_pos.items():
        ins = [d for d in group if d["entry"] in (DEAL_ENTRY_IN, DEAL_ENTRY_INOUT)]
        if not (pos_id in known_ids or (ins and ins[0]["magic"] == auto_magic)):
            continue
        if ins:
            last_entry = max(last_entry, ins[0]["time"])
            if ins[0]["time"] >= day_start:
                entries_today.append(ins[0]["time"])
        outs = [d for d in group if d["entry"] in (DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY)]
        if outs and pos_id not in open_ids and outs[-1]["time"] >= day_start:
            # a igual segundo decide el ticket del deal de cierre, que sí refleja el orden real
            closed.append((outs[-1]["time"], outs[-1]["ticket"], sum(deal_net(d) for d in group)))

    closed.sort()
    streak = 0
    for _, _, net in reversed(closed):
        if net < 0:
            streak += 1
        else:
            break
    return AutoStats(
        trades_today=len(entries_today),
        last_entry_time=last_entry,
        consecutive_losses=streak,
        closed_today=len(closed),
        wins_today=sum(1 for _, _, net in closed if net > 0),
        realized_today=round(sum(net for _, _, net in closed), 2),
        open_positions=auto_open,
    )


def mode_blockers(cfg: RiskConfig, acct: AccountSnapshot, halted: bool) -> list[str]:
    """Bloqueos que dejan inerte TODO el modo automático (abrir, cerrar y mover stops)."""
    reasons: list[str] = []
    if not cfg.auto.enabled:
        reasons.append(ENABLE_HINT)
    if halted:
        reasons.append("kill switch activo: el bot no toca nada hasta que el usuario borre HALT y reinicie el servidor")
    if not acct.is_demo and not (cfg.allow_live and cfg.auto.allow_live):
        reasons.append(
            f"la cuenta {acct.login} es {acct.mode_name}: el modo automático solo opera en DEMO salvo que el usuario "
            "active a mano allow_live y [auto] allow_live"
        )
    return reasons


def global_entry_blockers(cfg: RiskConfig, stats: AutoStats, server_now: float) -> list[str]:
    """Bloqueos para ABRIR que no dependen del símbolo: cupo diario, racha de pérdidas y enfriamiento."""
    a = cfg.auto
    reasons: list[str] = []
    if stats.trades_today >= a.max_trades_per_day:
        reasons.append(f"ya se abrieron {stats.trades_today} operaciones automáticas hoy (máximo {a.max_trades_per_day})")
    if stats.consecutive_losses >= a.max_consecutive_losses:
        reasons.append(
            f"{stats.consecutive_losses} pérdidas seguidas hoy (máximo {a.max_consecutive_losses}): sin más entradas hasta mañana (UTC)"
        )
    remaining = cooldown_remaining(cfg, stats, server_now)
    if remaining > 0:
        # hacia arriba, y nunca "0 min": a los 20 s de espera aún no se puede abrir
        left = "menos de 1" if remaining < 1 else str(math.ceil(remaining - 1e-9))
        reasons.append(f"enfriamiento: faltan {left} min desde la última entrada (mínimo {a.cooldown_minutes} min)")
    return reasons


def cooldown_remaining(cfg: RiskConfig, stats: AutoStats, server_now: float) -> float:
    """Minutos que faltan para poder abrir otra operación automática (0 si no hay espera)."""
    if not stats.last_entry_time or cfg.auto.cooldown_minutes <= 0:
        return 0.0
    # Si el reloj de este PC va unos segundos por detrás del servidor, la entrada parece "del futuro": la espera
    # nunca supera el mínimo configurado.
    elapsed = max(0.0, (server_now - stats.last_entry_time) / 60.0)
    return max(0.0, cfg.auto.cooldown_minutes - elapsed)


def symbol_entry_blockers(cfg: RiskConfig, symbol: str, all_position_symbols: list[str], quote_age: float | None,
                          just_arrived: bool = False) -> list[str]:
    """Bloqueos para ABRIR en un símbolo concreto: una posición por símbolo y cotización reciente.

    `just_arrived`: el servidor acaba de ver llegar esa cotización (su tick cambió entre dos lecturas cercanas).
    """
    a = cfg.auto
    reasons: list[str] = []
    if a.one_position_per_symbol and any(s.casefold() == symbol.casefold() for s in all_position_symbols):
        reasons.append(f"ya hay una posición abierta en {symbol} (una por símbolo)")
    if quote_age is None:
        # sin antigüedad: no se conoce la hora del servidor o el símbolo no ha cotizado nunca; no se afirma la causa
        reasons.append("no se puede comprobar que la cotización sea reciente (hora del servidor desconocida o símbolo sin cotización)")
    elif quote_age > a.max_quote_age_seconds:
        too_old = f"cotización de hace {quote_age:.0f} s (máximo {a.max_quote_age_seconds} s)"
        if just_arrived:
            # acaba de llegar y aun así parece vieja: no es un mercado parado, es el reloj con el que se mide
            reasons.append(f"{too_old} aunque acaba de llegar: el reloj de este PC va adelantado, o el terminal acaba de reconectar "
                           "y es la última de un mercado parado; si sigue igual con el mercado abierto, sincroniza la hora de Windows")
        else:
            reasons.append(f"{too_old}: mercado parado o sin ticks")
    return reasons


def rr_ratio(entry: float, sl: float, tp: float) -> float:
    risk = abs(entry - sl)
    return abs(tp - entry) / risk if risk > 0 else 0.0
