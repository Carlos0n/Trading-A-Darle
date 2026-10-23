"""Motor de riesgo: lógica pura, sin dependencia de MetaTrader5, para poder probarla en cualquier SO.

Todas las reglas se evalúan aquí. El modelo (Claude) solo propone; este módulo decide.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

from .config import RiskConfig

SIDE_BUY = "buy"
SIDE_SELL = "sell"

# Constantes de MetaTrader 5 reflejadas aquí para no importar el paquete en este módulo.
ACCOUNT_TRADE_MODE_DEMO = 0
ACCOUNT_TRADE_MODE_CONTEST = 1
ACCOUNT_TRADE_MODE_REAL = 2

SYMBOL_TRADE_MODE_DISABLED = 0
SYMBOL_TRADE_MODE_LONGONLY = 1
SYMBOL_TRADE_MODE_SHORTONLY = 2
SYMBOL_TRADE_MODE_CLOSEONLY = 3
SYMBOL_TRADE_MODE_FULL = 4

# Máscara filling_mode del símbolo (el paquete Python no define estas constantes; son literales de la doc MQL5)
SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2

# Valores de request["type_filling"] (sí existen en el paquete como mt5.ORDER_FILLING_*)
ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
ORDER_FILLING_RETURN = 2

ACCOUNT_MODE_NAMES = {
    ACCOUNT_TRADE_MODE_DEMO: "DEMO",
    ACCOUNT_TRADE_MODE_CONTEST: "CONTEST",
    ACCOUNT_TRADE_MODE_REAL: "REAL",
}


@dataclass(frozen=True)
class AccountSnapshot:
    login: int
    trade_mode: int
    balance: float
    equity: float
    currency: str
    server: str = ""
    leverage: int = 0
    margin_free: float = 0.0
    trade_allowed: bool = True
    trade_expert: bool = True

    @property
    def is_demo(self) -> bool:
        return self.trade_mode == ACCOUNT_TRADE_MODE_DEMO

    @property
    def mode_name(self) -> str:
        return ACCOUNT_MODE_NAMES.get(self.trade_mode, f"UNKNOWN({self.trade_mode})")


@dataclass(frozen=True)
class SymbolSpec:
    name: str
    digits: int
    point: float
    tick_size: float
    tick_value: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level: int = 0
    spread: int = 0
    filling_mode: int = 0
    trade_mode: int = SYMBOL_TRADE_MODE_FULL
    contract_size: float = 0.0
    currency_profit: str = ""
    tick_value_loss: float = 0.0

    @property
    def loss_tick_value(self) -> float:
        """Valor de un tick para una posición perdedora (trade_tick_value_loss); el mayor de ambos por prudencia."""
        return max(self.tick_value_loss, self.tick_value)


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str
    sl: float
    tp: float | None = None
    volume: float | None = None
    comment: str = ""
    risk_pct: float | None = None


@dataclass(frozen=True)
class RiskState:
    open_positions: int = 0
    total_volume: float = 0.0
    realized_today: float = 0.0
    floating_pnl: float = 0.0
    balance_ops_today: float = 0.0
    halted: bool = False
    trade_allowed: bool = True
    python_api_disabled: bool = False
    account_trade_allowed: bool = True
    account_expert_allowed: bool = True
    now_utc: datetime | None = None

    @property
    def can_trade(self) -> bool:
        return (
            self.trade_allowed
            and not self.python_api_disabled
            and self.account_trade_allowed
            and self.account_expert_allowed
        )


@dataclass
class Evaluation:
    ok: bool
    volume: float
    risk_amount: float
    risk_pct: float
    sl_points: float
    loss_per_lot: float
    entry_price: float = 0.0
    close_ref_price: float = 0.0
    sl: float = 0.0
    tp: float | None = None
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "volume": self.volume,
            "risk_amount": round(self.risk_amount, 2),
            "risk_pct": round(self.risk_pct, 3),
            "sl_points": self.sl_points,
            "loss_per_lot": round(self.loss_per_lot, 2),
            "entry_price": self.entry_price,
            "close_ref_price": self.close_ref_price,
            "sl": self.sl,
            "tp": self.tp,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
        }


class RiskError(ValueError):
    """Error de datos de entrada que impide evaluar una orden."""


def is_finite_positive(x: float | None) -> bool:
    try:
        return x is not None and math.isfinite(float(x)) and float(x) > 0
    except (TypeError, ValueError):
        return False


def normalize_side(side: str) -> str:
    s = (side or "").strip().lower()
    if s in ("buy", "long", "compra"):
        return SIDE_BUY
    if s in ("sell", "short", "venta"):
        return SIDE_SELL
    raise RiskError(f"side inválido: {side!r}; usa 'buy' o 'sell'")


def symbol_allowed(cfg: RiskConfig, name: str) -> bool:
    if not cfg.symbols:
        return True
    return name.casefold() in {s.casefold() for s in cfg.symbols}


def round_volume_down(spec: SymbolSpec, volume: float) -> float:
    """Redondea hacia abajo al múltiplo de volume_step con aritmética decimal exacta (nunca sube el volumen)."""
    if not is_finite_positive(volume):
        return 0.0
    if spec.volume_step <= 0:
        return float(volume)
    step = Decimal(str(spec.volume_step))
    v = Decimal(repr(float(volume)))
    # 1e-9 lotes de tolerancia absorbe el ruido de coma flotante (0.49999999999999994 -> 0.50) sin subir el volumen real
    return float(((v / step) + Decimal("1e-9")).to_integral_value(rounding=ROUND_FLOOR) * step)


def normalize_price(spec: SymbolSpec, px: float) -> float:
    """Ajusta un precio a la rejilla de trade_tick_size y a digits (los brokers rechazan precios fuera de rejilla)."""
    if spec.tick_size > 0:
        tick = Decimal(str(spec.tick_size))
        px = float((Decimal(str(px)) / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick)
    return round(px, spec.digits)


def loss_per_lot(spec: SymbolSpec, price: float, sl: float) -> float:
    """Pérdida en divisa de la cuenta si 1 lote toca el SL desde `price`."""
    tick_value = spec.loss_tick_value
    if spec.tick_size <= 0 or tick_value <= 0:
        raise RiskError(
            f"{spec.name}: trade_tick_size/trade_tick_value inválidos ({spec.tick_size}, {tick_value})"
        )
    distance = abs(Decimal(repr(float(price))) - Decimal(repr(float(sl))))
    if distance <= 0:
        raise RiskError("el SL no puede ser igual al precio de entrada")
    ticks = distance / Decimal(repr(float(spec.tick_size)))  # exacto: los precios viven en la rejilla de ticks
    return float(ticks * Decimal(repr(float(tick_value))))


def distance_points(spec: SymbolSpec, a: float, b: float) -> float:
    if spec.point <= 0:
        raise RiskError(f"{spec.name}: point inválido ({spec.point})")
    distance = abs(Decimal(repr(float(a))) - Decimal(repr(float(b))))
    return float(round(distance / Decimal(repr(float(spec.point))), 1))


def size_for_risk(
    cfg: RiskConfig,
    spec: SymbolSpec,
    equity: float,
    price: float,
    sl: float,
    risk_pct: float | None = None,
) -> tuple[float, float, float]:
    """Devuelve (volumen, riesgo_en_divisa, pérdida_por_lote) para arriesgar como máximo risk_pct del equity."""
    if risk_pct is not None and not is_finite_positive(risk_pct):
        raise RiskError("risk_pct debe ser un número > 0")
    pct = cfg.max_risk_pct_per_trade if risk_pct is None else min(float(risk_pct), cfg.max_risk_pct_per_trade)
    if not is_finite_positive(equity):
        raise RiskError(f"equity inválido ({equity}); no se puede dimensionar")
    risk_amount = equity * pct / 100.0
    lpl = loss_per_lot(spec, price, sl)
    raw = risk_amount / lpl
    volume = round_volume_down(spec, raw)
    volume = min(volume, cfg.max_volume_per_order, spec.volume_max)
    volume = round_volume_down(spec, volume)
    return volume, risk_amount, lpl


def pick_filling(spec: SymbolSpec) -> int:
    """Elige el modo de ejecución que el símbolo admite (máscara filling_mode del broker)."""
    if spec.filling_mode & SYMBOL_FILLING_FOK:
        return ORDER_FILLING_FOK
    if spec.filling_mode & SYMBOL_FILLING_IOC:
        return ORDER_FILLING_IOC
    return ORDER_FILLING_RETURN


def day_start_balance(acct: AccountSnapshot, state: RiskState) -> float:
    """Balance al empezar el día: el actual menos lo realizado hoy y menos depósitos/retiradas/bonos de hoy."""
    return acct.balance - state.realized_today - state.balance_ops_today


def pnl_today(state: RiskState) -> float:
    return state.realized_today + state.floating_pnl


def daily_loss_limit(cfg: RiskConfig, acct: AccountSnapshot, state: RiskState) -> float:
    """Umbral (negativo) de P&L del día a partir del cual se bloquea abrir posiciones."""
    return -abs(day_start_balance(acct, state)) * cfg.max_daily_loss_pct / 100.0


def daily_loss_hit(cfg: RiskConfig, acct: AccountSnapshot, state: RiskState) -> bool:
    return round(pnl_today(state), 2) <= round(daily_loss_limit(cfg, acct, state), 2)


def in_trading_hours(cfg: RiskConfig, now_utc: datetime | None) -> bool:
    if cfg.trading_hours_utc is None:
        return True
    now = now_utc or datetime.now(timezone.utc)
    start, end = cfg.trading_hours_utc
    hour = now.hour + now.minute / 60.0
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def stops_ok(side: str, close_ref: float, sl: float | None, tp: float | None) -> list[str]:
    """SL y TP deben estar del lado correcto del precio que los activa (bid para compras, ask para ventas)."""
    problems: list[str] = []
    if sl is not None:
        if side == SIDE_BUY and sl >= close_ref:
            problems.append(f"SL {sl} debe estar por debajo del bid {close_ref} (posición de compra)")
        if side == SIDE_SELL and sl <= close_ref:
            problems.append(f"SL {sl} debe estar por encima del ask {close_ref} (posición de venta)")
    if tp is not None:
        if side == SIDE_BUY and tp <= close_ref:
            problems.append(f"TP {tp} debe estar por encima del bid {close_ref} (posición de compra)")
        if side == SIDE_SELL and tp >= close_ref:
            problems.append(f"TP {tp} debe estar por debajo del ask {close_ref} (posición de venta)")
    return problems


def trading_blockers(cfg: RiskConfig, acct: AccountSnapshot, state: RiskState) -> list[str]:
    """Razones que impiden abrir cualquier posición, independientes de la orden concreta."""
    reasons: list[str] = []
    if state.halted:
        reasons.append(
            f"trading detenido (kill switch): existe o existió el archivo {cfg.halt_file}. "
            "Para reanudar, un humano debe borrarlo y reiniciar el servidor MCP (/mcp)"
        )
    if not state.trade_allowed:
        reasons.append("AutoTrading está desactivado en el terminal MT5 (botón 'Algo Trading'); actívalo a mano")
    if state.python_api_disabled:
        reasons.append(
            "MT5 tiene marcada 'Deshabilitar el trading automático a través de la API externa de Python' "
            "(Herramientas > Opciones > Asesores Expertos); desmárcala a mano"
        )
    if not state.account_trade_allowed:
        reasons.append("el broker tiene el trading deshabilitado para esta cuenta (account trade_allowed=false)")
    if not state.account_expert_allowed:
        reasons.append("el broker no permite trading automático en esta cuenta (account trade_expert=false)")
    if not acct.is_demo and not cfg.allow_live:
        reasons.append(
            f"la cuenta {acct.login} es {acct.mode_name} y allow_live=false en la configuración; solo se permite DEMO"
        )
    if not in_trading_hours(cfg, state.now_utc):
        reasons.append(
            f"fuera del horario permitido (UTC {cfg.trading_hours_utc[0]:02d}:00-{cfg.trading_hours_utc[1]:02d}:00)"
        )
    if daily_loss_hit(cfg, acct, state):
        reasons.append(
            f"límite de pérdida diaria alcanzado: P&L hoy {pnl_today(state):.2f} {acct.currency} "
            f"<= {daily_loss_limit(cfg, acct, state):.2f} ({cfg.max_daily_loss_pct}% del balance inicial del día)"
        )
    if state.open_positions >= cfg.max_open_positions:
        reasons.append(f"ya hay {state.open_positions} posiciones abiertas (máximo {cfg.max_open_positions})")
    return reasons


def evaluate_order(
    cfg: RiskConfig,
    acct: AccountSnapshot,
    spec: SymbolSpec,
    state: RiskState,
    intent: OrderIntent,
    bid: float,
    ask: float,
) -> Evaluation:
    """Evalúa una orden de mercado contra TODAS las reglas y calcula el volumen permitido.

    Devuelve siempre un Evaluation; `ok=False` incluye todas las razones encontradas.
    La entrada se hace al ask (compra) o al bid (venta); los stops se validan contra el precio que los
    activa (bid para compras, ask para ventas), que es lo que exige el broker.
    """
    reasons: list[str] = list(trading_blockers(cfg, acct, state))
    warnings: list[str] = []
    side = normalize_side(intent.side)
    symbol = spec.name

    if not symbol_allowed(cfg, symbol):
        reasons.append(f"{symbol} no está en la lista blanca de símbolos: {', '.join(cfg.symbols)}")

    if spec.trade_mode in (SYMBOL_TRADE_MODE_DISABLED, SYMBOL_TRADE_MODE_CLOSEONLY):
        reasons.append(f"{symbol}: el broker no permite abrir posiciones ahora (trade_mode={spec.trade_mode})")
    elif spec.trade_mode == SYMBOL_TRADE_MODE_LONGONLY and side == SIDE_SELL:
        reasons.append(f"{symbol}: solo se permiten compras (long only)")
    elif spec.trade_mode == SYMBOL_TRADE_MODE_SHORTONLY and side == SIDE_BUY:
        reasons.append(f"{symbol}: solo se permiten ventas (short only)")

    prices_ok = is_finite_positive(bid) and is_finite_positive(ask) and ask >= bid
    if not prices_ok:
        reasons.append(f"cotización inválida (bid={bid}, ask={ask}); ¿mercado cerrado o símbolo sin cotización?")
    entry = (ask if side == SIDE_BUY else bid) if prices_ok else 0.0
    close_ref = (bid if side == SIDE_BUY else ask) if prices_ok else 0.0

    sl_ok = is_finite_positive(intent.sl)
    if not sl_ok:
        reasons.append("stop loss obligatorio (sl debe ser un número > 0)")
    tp: float | None = None
    if intent.tp is not None:
        if not is_finite_positive(intent.tp):
            reasons.append("take profit inválido (tp debe ser un número > 0 o no indicarse)")
        else:
            tp = float(intent.tp)
    if cfg.require_tp and tp is None:
        reasons.append("take profit obligatorio (require_tp=true)")
    if intent.volume is not None and not is_finite_positive(intent.volume):
        reasons.append("volumen inválido (volume debe ser un número > 0 o no indicarse)")
    if intent.risk_pct is not None and not is_finite_positive(intent.risk_pct):
        reasons.append("risk_pct inválido (debe ser un número > 0 o no indicarse)")

    sl_points = 0.0
    lpl = 0.0
    volume = 0.0
    risk_amount = 0.0
    risk_pct = 0.0
    sl = float(intent.sl) if sl_ok else 0.0

    if prices_ok and sl_ok:
        reasons.extend(stops_ok(side, close_ref, sl, tp))
        try:
            min_points = max(spec.stops_level, cfg.min_sl_points)
            sl_from_close = distance_points(spec, close_ref, sl)
            if sl_from_close < min_points:
                reasons.append(
                    f"SL demasiado cerca: {sl_from_close} puntos del precio de cierre ({close_ref}); mínimo {min_points}"
                )
            if tp is not None and spec.stops_level > 0 and distance_points(spec, close_ref, tp) < spec.stops_level:
                reasons.append(
                    f"TP demasiado cerca: {distance_points(spec, close_ref, tp)} puntos del precio de cierre; "
                    f"mínimo {spec.stops_level}"
                )
            sl_points = distance_points(spec, entry, sl)
            lpl = loss_per_lot(spec, entry, sl)
            volume_arg = float(intent.volume) if is_finite_positive(intent.volume) else None
            risk_arg = float(intent.risk_pct) if is_finite_positive(intent.risk_pct) else None
            max_vol, max_risk_amount, _ = size_for_risk(cfg, spec, acct.equity, entry, sl, risk_arg)
            if volume_arg is None:
                volume = max_vol
            else:
                volume = round_volume_down(spec, volume_arg)
                if volume > max_vol + 1e-12:
                    reasons.append(
                        f"volumen {volume_arg} supera el máximo permitido por riesgo/límites: {max_vol} lotes"
                    )
            risk_amount = volume * lpl
            risk_pct = (risk_amount / acct.equity * 100.0) if acct.equity > 0 else 0.0
            if volume < spec.volume_min:
                reasons.append(
                    f"el presupuesto de riesgo ({max_risk_amount:.2f} {acct.currency}) no alcanza ni para el lote mínimo "
                    f"{spec.volume_min} (pérdida por lote mínimo {lpl * spec.volume_min:.2f}); acerca el SL o reduce la operación"
                )
            if volume > cfg.max_volume_per_order + 1e-12:
                reasons.append(f"volumen {volume} supera max_volume_per_order={cfg.max_volume_per_order}")
            if state.total_volume + volume > cfg.max_total_volume + 1e-12:
                reasons.append(
                    f"volumen total abierto {state.total_volume} + {volume} supera max_total_volume={cfg.max_total_volume}"
                )
        except RiskError as exc:
            reasons.append(str(exc))

    if cfg.max_spread_points > 0 and spec.spread > cfg.max_spread_points:
        reasons.append(f"spread {spec.spread} puntos supera max_spread_points={cfg.max_spread_points}")

    if spec.spread > 0 and sl_points > 0 and spec.spread > sl_points / 3:
        warnings.append(f"el spread ({spec.spread} puntos) es más de un tercio de la distancia al SL ({sl_points})")
    if tp is not None and sl_ok and prices_ok:
        reward = abs(tp - entry)
        risk = abs(entry - sl)
        if risk > 0 and reward / risk < 1.0:
            warnings.append(f"ratio beneficio/riesgo {reward / risk:.2f} < 1")

    return Evaluation(
        ok=not reasons,
        volume=volume,
        risk_amount=risk_amount,
        risk_pct=risk_pct,
        sl_points=sl_points,
        loss_per_lot=lpl,
        entry_price=entry,
        close_ref_price=close_ref,
        sl=sl,
        tp=tp,
        reasons=reasons,
        warnings=warnings,
    )
