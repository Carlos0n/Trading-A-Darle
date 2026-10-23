"""Motor de riesgo: lógica pura, sin dependencia de MetaTrader5, para poder probarla en cualquier SO.

Todas las reglas se evalúan aquí. El modelo (Claude) solo propone; este módulo decide.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

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

SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2

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
    halted: bool = False
    trade_allowed: bool = True
    now_utc: datetime | None = None


@dataclass
class Evaluation:
    ok: bool
    volume: float
    risk_amount: float
    risk_pct: float
    sl_points: float
    loss_per_lot: float
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
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
        }


class RiskError(ValueError):
    """Error de datos de entrada que impide evaluar una orden."""


def normalize_side(side: str) -> str:
    s = (side or "").strip().lower()
    if s in ("buy", "long", "compra"):
        return SIDE_BUY
    if s in ("sell", "short", "venta"):
        return SIDE_SELL
    raise RiskError(f"side inválido: {side!r}; usa 'buy' o 'sell'")


def _step_decimals(step: float) -> int:
    if step <= 0 or step >= 1:
        return 0
    return max(0, -int(math.floor(math.log10(step) + 1e-9)))


def round_volume_down(spec: SymbolSpec, volume: float) -> float:
    """Redondea hacia abajo al múltiplo de volume_step (nunca arriesga más de lo pedido)."""
    if volume <= 0:
        return 0.0
    if spec.volume_step <= 0:
        return volume
    steps = math.floor(volume / spec.volume_step + 1e-9)
    return round(steps * spec.volume_step, _step_decimals(spec.volume_step))


def loss_per_lot(spec: SymbolSpec, price: float, sl: float) -> float:
    """Pérdida en divisa de la cuenta si 1 lote toca el SL desde `price`."""
    if spec.tick_size <= 0 or spec.tick_value <= 0:
        raise RiskError(
            f"{spec.name}: trade_tick_size/trade_tick_value inválidos ({spec.tick_size}, {spec.tick_value})"
        )
    distance = abs(price - sl)
    if distance <= 0:
        raise RiskError("el SL no puede ser igual al precio de entrada")
    return (distance / spec.tick_size) * spec.tick_value


def sl_distance_points(spec: SymbolSpec, price: float, sl: float) -> float:
    if spec.point <= 0:
        raise RiskError(f"{spec.name}: point inválido ({spec.point})")
    return round(abs(price - sl) / spec.point, 1)


def size_for_risk(
    cfg: RiskConfig,
    spec: SymbolSpec,
    equity: float,
    price: float,
    sl: float,
    risk_pct: float | None = None,
) -> tuple[float, float, float]:
    """Devuelve (volumen, riesgo_en_divisa, pérdida_por_lote) para arriesgar como máximo risk_pct del equity."""
    pct = cfg.max_risk_pct_per_trade if risk_pct is None else min(float(risk_pct), cfg.max_risk_pct_per_trade)
    if pct <= 0:
        raise RiskError("risk_pct debe ser > 0")
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
    return acct.balance - state.realized_today


def pnl_today(state: RiskState) -> float:
    return state.realized_today + state.floating_pnl


def daily_loss_limit(cfg: RiskConfig, acct: AccountSnapshot, state: RiskState) -> float:
    """Umbral (negativo) de P&L del día a partir del cual se bloquea abrir posiciones."""
    return -abs(day_start_balance(acct, state)) * cfg.max_daily_loss_pct / 100.0


def daily_loss_hit(cfg: RiskConfig, acct: AccountSnapshot, state: RiskState) -> bool:
    return pnl_today(state) <= daily_loss_limit(cfg, acct, state)


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


def stops_ok(side: str, price: float, sl: float | None, tp: float | None) -> list[str]:
    """SL y TP deben estar del lado correcto del precio."""
    problems: list[str] = []
    if sl is not None:
        if side == SIDE_BUY and sl >= price:
            problems.append(f"SL {sl} debe estar por debajo del precio de compra {price}")
        if side == SIDE_SELL and sl <= price:
            problems.append(f"SL {sl} debe estar por encima del precio de venta {price}")
    if tp is not None:
        if side == SIDE_BUY and tp <= price:
            problems.append(f"TP {tp} debe estar por encima del precio de compra {price}")
        if side == SIDE_SELL and tp >= price:
            problems.append(f"TP {tp} debe estar por debajo del precio de venta {price}")
    return problems


def evaluate_order(
    cfg: RiskConfig,
    acct: AccountSnapshot,
    spec: SymbolSpec,
    state: RiskState,
    intent: OrderIntent,
    price: float,
) -> Evaluation:
    """Evalúa una orden de mercado contra TODAS las reglas y calcula el volumen permitido.

    Devuelve siempre un Evaluation; `ok=False` incluye todas las razones encontradas.
    """
    reasons: list[str] = []
    warnings: list[str] = []
    side = normalize_side(intent.side)
    symbol = spec.name.upper()

    if state.halted:
        reasons.append(f"trading detenido: existe el archivo {cfg.halt_file} (kill switch). Bórralo a mano para reanudar")
    if not state.trade_allowed:
        reasons.append("AutoTrading está desactivado en el terminal MT5 (botón 'Algo Trading'); actívalo a mano")
    if not acct.is_demo and not cfg.allow_live:
        reasons.append(
            f"la cuenta {acct.login} es {acct.mode_name} y allow_live=false en risk.toml; solo se permite DEMO"
        )
    if cfg.symbols and symbol not in cfg.symbols:
        reasons.append(f"{symbol} no está en la lista blanca de símbolos: {', '.join(cfg.symbols)}")

    if spec.trade_mode in (SYMBOL_TRADE_MODE_DISABLED, SYMBOL_TRADE_MODE_CLOSEONLY):
        reasons.append(f"{symbol}: el broker no permite abrir posiciones ahora (trade_mode={spec.trade_mode})")
    elif spec.trade_mode == SYMBOL_TRADE_MODE_LONGONLY and side == SIDE_SELL:
        reasons.append(f"{symbol}: solo se permiten compras (long only)")
    elif spec.trade_mode == SYMBOL_TRADE_MODE_SHORTONLY and side == SIDE_BUY:
        reasons.append(f"{symbol}: solo se permiten ventas (short only)")

    if not in_trading_hours(cfg, state.now_utc):
        reasons.append(f"fuera del horario permitido (UTC {cfg.trading_hours_utc[0]:02d}:00-{cfg.trading_hours_utc[1]:02d}:00)")

    if price <= 0:
        reasons.append("precio de mercado inválido (0); ¿mercado cerrado o símbolo sin cotización?")

    if intent.sl is None or intent.sl <= 0:
        reasons.append("stop loss obligatorio (sl > 0)")
    if cfg.require_tp and (intent.tp is None or intent.tp <= 0):
        reasons.append("take profit obligatorio (require_tp=true)")

    sl_points = 0.0
    lpl = 0.0
    volume = 0.0
    risk_amount = 0.0
    risk_pct = 0.0

    if price > 0 and intent.sl and intent.sl > 0:
        reasons.extend(stops_ok(side, price, intent.sl, intent.tp if intent.tp else None))
        try:
            sl_points = sl_distance_points(spec, price, intent.sl)
            min_points = max(spec.stops_level, cfg.min_sl_points)
            if sl_points < min_points:
                reasons.append(f"SL demasiado cerca: {sl_points} puntos; mínimo {min_points}")
            lpl = loss_per_lot(spec, price, intent.sl)
            max_vol, max_risk_amount, _ = size_for_risk(cfg, spec, acct.equity, price, intent.sl, intent.risk_pct)
            if intent.volume is None:
                volume = max_vol
            else:
                volume = round_volume_down(spec, float(intent.volume))
                if volume > max_vol + 1e-12:
                    reasons.append(
                        f"volumen {intent.volume} supera el máximo permitido por riesgo/límites: {max_vol} lotes"
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

    if daily_loss_hit(cfg, acct, state):
        reasons.append(
            f"límite de pérdida diaria alcanzado: P&L hoy {pnl_today(state):.2f} {acct.currency} "
            f"<= {daily_loss_limit(cfg, acct, state):.2f} ({cfg.max_daily_loss_pct}% del balance inicial del día)"
        )
    if state.open_positions >= cfg.max_open_positions:
        reasons.append(f"ya hay {state.open_positions} posiciones abiertas (máximo {cfg.max_open_positions})")

    if spec.spread > 0 and sl_points > 0 and spec.spread > sl_points / 3:
        warnings.append(f"el spread ({spec.spread} puntos) es más de un tercio de la distancia al SL ({sl_points})")
    if intent.tp and intent.sl and price > 0:
        reward = abs(intent.tp - price)
        risk = abs(price - intent.sl)
        if risk > 0 and reward / risk < 1.0:
            warnings.append(f"ratio beneficio/riesgo {reward / risk:.2f} < 1")

    return Evaluation(
        ok=not reasons,
        volume=volume,
        risk_amount=risk_amount,
        risk_pct=risk_pct,
        sl_points=sl_points,
        loss_per_lot=lpl,
        reasons=reasons,
        warnings=warnings,
    )
