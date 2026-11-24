"""Lógica de las herramientas MCP (independiente del transporte, para poder probarla)."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import auto as auto_rules
from .broker import (
    DEAL_TYPE_BUY,
    DEAL_TYPE_SELL,
    DEAL_TYPES_BALANCE_OPS,
    TF_SECONDS,
    BrokerError,
    MT5Broker,
    normalize_timeframe,
    retcode_name,
)
from .config import PROJECT_ROOT, RiskConfig, load_config
from .indicators import atr, ema, rsi
from .journal import Journal
from .risk import (
    SIDE_BUY,
    AccountSnapshot,
    Evaluation,
    OrderIntent,
    RiskError,
    RiskState,
    daily_loss_hit,
    daily_loss_limit,
    distance_points,
    evaluate_order,
    is_finite_positive,
    normalize_price,
    normalize_side,
    pick_filling,
    pnl_today,
    size_for_risk,
    stops_ok,
    symbol_allowed,
    trading_blockers,
)
from .timeutil import MAX_BEHIND, MIN_OFFSET, ServerClock

RETCODE_DONE = 10009
RETCODE_PLACED = 10008
RETCODE_DONE_PARTIAL = 10010

LOOKBACK_DAYS = 30  # histórico que se lee para atribuir al bot posiciones abiertas en días anteriores
AUTO_IDS_FILE = "auto_positions.jsonl"  # registro propio de las posiciones que abre el bot (junto al journal)
AUTO_IDS_KEEP_DAYS = 120
STALE_SECONDS = 300  # una cotización más vieja que esto se marca como parada
CLOCK_WAIT_SECONDS = 6.0  # espera máxima a que un tick se mueva para fijar la hora del servidor
CLOCK_POLL_SECONDS = 0.5  # cada cuánto se releen los ticks durante esa espera
WATCH_REFS = 20  # símbolos de Observación de Mercado que sirven de referencia del reloj si la lista blanca está vacía
RESOLVE_RETRY_SECONDS = 60.0  # sin hora del servidor, cada cuánto se vuelve a buscar un nombre de símbolo que no resolvió
OLD_DEALS_KEEP = 200  # posiciones antiguas cerradas cuyo histórico completo se recuerda
WARMUP_BARS = 260  # velas que se leen para que EMA/RSI/ATR estén estabilizados
MIN_REASON_CHARS = 15
NO_RESPONSE = "SIN_RESPUESTA"  # retcode_name cuando order_send no devolvió nada

CONFIRM_MSG = (
    "Orden NO enviada: falta confirm=true. Muestra al usuario el resumen (símbolo, lado, volumen, SL, TP, "
    "riesgo en divisa y %) y llama de nuevo con confirm=true y el mismo volume SOLO si el usuario confirma explícitamente."
)
REASON_MSG = (
    f"falta el motivo (reason): al menos {MIN_REASON_CHARS} caracteres con la regla de la estrategia que se cumple"
)
SYMBOLS_EMPTY_MSG = "el modo automático necesita una lista `symbols` no vacía en la configuración de riesgo"
SYMBOLS_UNKNOWN_MSG = (
    "ningún símbolo de `symbols` existe con ese nombre en este broker: usa los nombres exactos de Observación de Mercado"
)
UNKNOWN_RESULT_MSG = (
    "order_send no devolvió resultado ({exc}): la orden PUDO llegar al broker. "
    "Verifica con positions() cómo quedó la posición y no la repitas"
)
DAY_UTC = "el día va de 00:00 a 24:00 UTC"
DAY_UNVERIFIED = (
    "ventana del día SIN VERIFICAR (hora del servidor desconocida): los importes de hoy pueden incluir operaciones "
    "de ayer o dejar fuera alguna de hoy"
)
HALT_BEFORE_SEND_MSG = (
    "Orden NO enviada: el kill switch se activó mientras se preparaba la orden. No se abre nada hasta que un humano "
    "borre el archivo {halt_file} y reinicie el servidor MCP"
)
# Eventos del journal de una apertura: rechazo del order_check del broker, kill switch activado mientras se preparaba,
# intento, enviada, fallida y sin respuesta.
ORDER_EVENTS = {"check": "order_rejected_by_broker_check", "halt": "order_rejected_by_halt", "attempt": "order_attempt",
                "sent": "order_sent", "failed": "order_failed", "unknown": "order_unknown"}
AUTO_ORDER_EVENTS = {"check": "auto_rejected_by_broker_check", "halt": "auto_rejected_by_halt", "attempt": "auto_order_attempt",
                     "sent": "auto_order_sent", "failed": "auto_order_failed", "unknown": "auto_order_unknown"}


def _ago(seconds: float) -> str:
    """Antigüedad legible: segundos hasta 2 minutos, minutos hasta 2 horas y horas a partir de ahí."""
    if seconds < 120:
        return f"{seconds:.0f} s"
    return f"{seconds / 60:.0f} min" if seconds < 7200 else f"{seconds / 3600:.0f} h"


def _status(code: int) -> str:
    return {RETCODE_DONE: "filled", RETCODE_DONE_PARTIAL: "partial", RETCODE_PLACED: "placed"}.get(code, "failed")


@dataclass
class Snapshot:
    """Foto coherente del terminal para una llamada: una sola lectura de cuenta, posiciones e histórico."""

    now: datetime
    acct: AccountSnapshot
    term: dict
    positions: list[dict]
    day_start: int  # inicio del día UTC expresado en el reloj del servidor (sin verificar si no se conoce su hora)
    state: RiskState
    auto: auto_rules.AutoStats
    server_now: float | None = None  # hora del servidor en el momento de la foto; None si no se conoce
    clock_blocker: str | None = None  # por qué no se conoce la hora del servidor; None si se conoce

    @property
    def utc_ts(self) -> float:
        return self.now.timestamp()


class Engine:
    def __init__(
        self,
        broker: MT5Broker,
        cfg: RiskConfig | None = None,
        root: Path | None = None,
        journal: Journal | None = None,
        latch_halt: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        clock_wait: float = CLOCK_WAIT_SECONDS,
    ):
        self.b = broker
        self.cfg = cfg or load_config()
        self.root = Path(root or PROJECT_ROOT)
        self.journal = journal or Journal(self.root / self.cfg.journal_file)
        self.clock = ServerClock()
        # El servidor MCP enclava la parada: una vez visto HALT sigue parado hasta reiniciarlo. El panel no envía
        # órdenes y usa latch_halt=False para mostrar solo si el archivo HALT existe ahora.
        self._latch_halt = latch_halt
        # Espera máxima a que un tick se mueva cuando aún no se conoce la hora del servidor (0 = no esperar).
        self._sleep = sleep
        self._clock_wait = clock_wait
        self._halt_latched = False
        self._resolved: dict[str, str | None] = {}
        self._resolve_retry_at = 0.0  # última vez (UTC) que se olvidaron los nombres que no resolvieron
        self._watch: list[str] | None = None  # símbolos de Observación de Mercado usados como referencia del reloj
        self._old_deals: dict[int, list[dict]] = {}  # histórico completo de posiciones antiguas ya cerradas
        self._auto_ids_cache: tuple[float, frozenset[int]] = (-1.0, frozenset())

    # ---- helpers -------------------------------------------------------------------------------
    @property
    def halt_path(self) -> Path:
        return self.root / self.cfg.halt_file

    def halted(self) -> bool:
        """Kill switch enclavado: una vez visto el archivo HALT, sigue activo hasta reiniciar el servidor."""
        exists = self.halt_path.exists()
        if not self._latch_halt:
            return exists
        if exists:
            self._halt_latched = True
        return self._halt_latched

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    # Registro de posiciones del bot. El magic identifica sus órdenes, pero hay brokers que lo borran de la
    # posición o de los deals; este archivo permite seguir contando cupo, racha y propiedad aunque eso ocurra.
    @property
    def auto_ids_path(self) -> Path:
        return self.journal.path.parent / AUTO_IDS_FILE

    def _auto_ids(self) -> frozenset[int]:
        path = self.auto_ids_path
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return frozenset()
        if self._auto_ids_cache[0] == mtime:
            return self._auto_ids_cache[1]
        cutoff = self._now().timestamp() - AUTO_IDS_KEEP_DAYS * 86400
        ids: set[int] = set()
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return self._auto_ids_cache[1]  # ilegible ahora mismo: vale lo último que se leyó (el magic sigue contando)
        for line in lines:
            try:
                rec = json.loads(line)
                if float(rec.get("ts", 0)) >= cutoff and int(rec["id"]) > 0:
                    ids.add(int(rec["id"]))
            except (ValueError, KeyError, TypeError):
                continue
        self._auto_ids_cache = (mtime, frozenset(ids))
        return self._auto_ids_cache[1]

    def _register_auto_position(self, position_id: Any) -> str | None:
        """Anota la posición en el registro del bot. La orden ya está enviada, así que nunca lanza: devuelve el error."""
        try:
            pid = int(position_id or 0)
        except (TypeError, ValueError):
            return None
        if pid <= 0:
            return None
        try:
            path = self.auto_ids_path
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"id": pid, "ts": round(self._now().timestamp(), 1)}) + "\n")
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"
        finally:
            self._auto_ids_cache = (-1.0, frozenset())
        return None

    def _with_journal(self, out: dict, event: str, **data: Any) -> dict:
        """Anota en el journal sin que un fallo de escritura cambie el resultado real: lo añade como journal_error."""
        try:
            self.journal.record(event, **data)
        except Exception as exc:
            out["journal_error"] = f"no se pudo anotar en el journal: {type(exc).__name__}: {exc}"
        return out

    def _owner(self, position: dict) -> str:
        if auto_rules.is_bot_position(position, self.cfg.auto_magic, self._auto_ids()):
            return "auto"
        return "manual" if position.get("magic") == self.cfg.magic else "externo"

    def _mt5(self) -> Any:
        return self.b.mt5

    def _watch_symbols(self) -> list[str]:
        if self._watch is None:
            try:
                self._watch = self.b.visible_symbols(WATCH_REFS) or None
            except BrokerError:
                return []
        return self._watch or []

    def _ref_tick_times(self, extra: list[str]) -> dict[str, int]:
        """Tiempo del último tick de cada símbolo de referencia (lista blanca + los indicados), por nombre exacto.

        Con la lista blanca vacía ("cualquier símbolo") se añaden los visibles en Observación de Mercado, para que
        el reloj tenga referencias aunque no haya posiciones.
        """
        times: dict[str, int] = {}

        def read(exact: str) -> bool:
            if exact not in times:
                try:
                    times[exact] = self.b.tick(exact)["time"]
                except BrokerError:
                    return False
            return True

        for name in dict.fromkeys([*self.cfg.symbols, *extra]):
            if name not in self._resolved:
                try:
                    self._resolved[name] = self.b.symbol(name).name
                except BrokerError:
                    self._resolved[name] = None  # no existe en este broker: no se reintenta en cada lectura de ticks
            exact = self._resolved[name]
            if exact is not None:
                read(exact)
        if not self.cfg.symbols:
            if not [name for name in self._watch_symbols() if read(name)]:
                self._watch = None  # ninguno sirvió (cambió Observación de Mercado): se vuelve a pedir la lista
        return times

    def _sync_clock(self, extra: list[str], utc_ts: float) -> bool:
        """Actualiza la hora del servidor con una lectura de ticks. Devuelve True si tuvo que esperar.

        El desfase solo se acepta con ticks que se mueven entre lecturas cercanas (ver timeutil). Si aún no se conoce
        y no hay una lectura reciente con la que comparar (proceso recién arrancado o tras una pausa larga), se
        releen los ticks cada medio segundo hasta que la hora quede fijada o pase `clock_wait`. Con el mercado parado
        ninguno se mueve y la hora queda desconocida; mientras haya una lectura reciente no se vuelve a esperar,
        salvo que en esta misma lectura se haya movido un tick y solo falte otro que lo confirme (mercado que reabre).
        """
        had_recent = self.clock.has_recent_reading(utc_ts)
        if not self.clock.known and utc_ts - self._resolve_retry_at >= RESOLVE_RETRY_SECONDS:
            # Un nombre que no resolvió pudo fallar por algo pasajero (terminal arrancando, símbolos aún sin cargar):
            # mientras no haya hora se vuelve a buscar cada cierto tiempo, en vez de darlo por inexistente hasta
            # reiniciar el proceso (el panel y una sesión de Claude Code viven horas).
            self._resolve_retry_at = utc_ts
            self._resolved = {name: exact for name, exact in self._resolved.items() if exact is not None}
        ticks = self._ref_tick_times(extra)
        self.clock.update(ticks, utc_ts)
        just_moved = self.clock.pending is not None and self.clock.moved_at == utc_ts
        waited = 0.0
        if not self.clock.known and ticks and (just_moved or not had_recent):
            while waited < self._clock_wait:
                step = min(CLOCK_POLL_SECONDS, self._clock_wait - waited)
                self._sleep(step)
                waited += step
                now = self._now().timestamp()
                self.clock.update(self._ref_tick_times(extra), now)
                if self.clock.known or (self.clock.is_live(now) and self.clock.pending is None):
                    break  # ya hay hora, o llegan ticks que no encajan con el reloj del PC: esperar más no lo arregla
        if not self.clock.known:
            self._watch = None  # lista blanca vacía: la próxima vez se vuelven a elegir los símbolos que más cotizan
        return waited > 0

    def _clock_blocker(self, utc_ts: float) -> str | None:
        """Por qué no se conoce la hora del servidor, o None si se conoce. Nunca se supone que el servidor va en UTC."""
        if self.clock.known:
            return None
        if self.clock.pending is not None and self.clock.is_live(utc_ts):
            cause = ("ha llegado una cotización tras un rato sin ninguna y hace falta otra para confirmar la hora; "
                     "repite la consulta en unos segundos")
        elif self.clock.is_live(utc_ts):
            # Un tick que cambia y no encaja no dice por qué: puede ser el reloj del PC, o el último tick del servidor
            # que el terminal recibe al reconectar con el mercado parado. No se afirma una sola causa.
            cause = ("llegan cotizaciones, pero su hora no encaja con el reloj de este PC: o son antiguas (el terminal acaba "
                     "de reconectar con el mercado parado) o la hora de Windows está desajustada; si sigue igual con el "
                     "mercado abierto, sincroniza la hora de Windows")
        elif not self.clock.prev:
            cause = "no hay ningún símbolo de referencia con cotización; revisa `symbols` en la configuración y Observación de Mercado"
        else:
            cause = ("no llega ninguna cotización nueva (mercado cerrado o terminal sin conexión con el broker; "
                     "si el símbolo cotiza poco, repite la consulta en unos segundos)")
        return (
            f"hora del servidor desconocida: {cause}. Sin ella no se puede delimitar el día ni medir la antigüedad de las "
            "cotizaciones: no se abren posiciones (cerrar y modificar sigue permitido)"
        )

    def _whitelist_quiet_seconds(self, server_now: float) -> float | None:
        """Segundos desde el tick más reciente de los símbolos de la lista blanca (última lectura); None si no hay ninguno."""
        times = [self.clock.prev[exact] for name in self.cfg.symbols if (exact := self._resolved.get(name)) and self.clock.prev.get(exact)]
        return max(0.0, server_now - max(times)) if times else None

    def _just_arrived(self, exact: str, age: float | None, utc_ts: float) -> bool:
        """¿Acaba de llegar la cotización de `exact` y aun así, con el reloj de este PC, parece tener `age` segundos?

        Solo si se la ha visto cambiar después de una lectura hecha hace menos del máximo de [auto] (llegó a tiempo;
        +1 s porque el tiempo de un tick viene en segundos enteros) y su retraso aparente cabe en el adelanto del
        reloj del PC con el que la hora del servidor se da por conocida (MAX_BEHIND). Un tick que cambia y trae más
        retraso es el último del servidor, recibido al reconectar con el mercado parado: ese no acaba de llegar.
        """
        arrived = self.clock.arrived_within(exact, utc_ts)
        if arrived is None or age is None:
            return False
        return arrived + 1 <= self.cfg.auto.max_quote_age_seconds and age <= arrived + MAX_BEHIND + 1

    def _clock_info(self) -> dict:
        return {
            "server_clock_known": self.clock.known,
            "server_utc_offset_hours": None if self.clock.offset is None else self.clock.offset / 3600.0,
        }

    def _stats_deals(self, trades: list[dict], positions: list[dict], day_start: int) -> list[dict]:
        """Deals con los que se calculan las estadísticas del bot.

        La autoría de una posición cerrada se decide por su primer deal de entrada. Si la posición se abrió antes de
        la ventana leída y se cerró hoy, ese deal no está: se pide el histórico completo de esa posición (una vez;
        ya está cerrada y no cambia) para que su cierre cuente en la racha y en las estadísticas del día.
        """
        open_ids = {p.get("identifier", p["ticket"]) for p in positions}
        with_entry: set[int] = set()
        closed_today: set[int] = set()
        for d in trades:
            if d["entry"] == auto_rules.DEAL_ENTRY_IN:
                with_entry.add(d["position_id"])
            elif d["time"] >= day_start:
                closed_today.add(d["position_id"])
        missing = sorted(pid for pid in closed_today - with_entry - open_ids if pid > 0)
        if not missing:
            return trades
        out = list(trades)
        for pid in missing:
            if pid not in self._old_deals:
                if len(self._old_deals) >= OLD_DEALS_KEEP:
                    self._old_deals.clear()
                self._old_deals[pid] = [d for d in self.b.position_deals(pid) if d["type"] in (DEAL_TYPE_BUY, DEAL_TYPE_SELL)]
            seen = {d["ticket"] for d in trades if d["position_id"] == pid}
            out.extend(d for d in self._old_deals[pid] if d["ticket"] not in seen)  # sin duplicar los que ya estaban
        return out

    def _snapshot(self, extra_symbols: list[str] | None = None) -> Snapshot:
        now = self._now()
        positions = self.b.positions()
        if self._sync_clock([*(extra_symbols or []), *(p["symbol"] for p in positions)], now.timestamp()):
            # hubo que esperar a que se moviera un tick: la foto se toma después de la espera
            now = self._now()
            positions = self.b.positions()
        utc_ts = now.timestamp()
        acct = self.b.account()
        term = self.b.terminal()
        # Con la hora del servidor desconocida se usa desfase 0 solo para poder mostrar algo: la ventana del día
        # queda marcada como no verificada (clock_blocker) y no se abre ninguna posición con ella.
        offset = self.clock.offset or 0
        midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc).timestamp()
        day_start = int(midnight + offset)
        raw = self.b.deals(day_start - LOOKBACK_DAYS * 86400, int(utc_ts + offset) + 86400)
        trades = [d for d in raw if d["type"] in (DEAL_TYPE_BUY, DEAL_TYPE_SELL)]
        # Resultado del día: operaciones más cualquier cargo del broker (comisiones diarias, intereses, ajustes).
        # Solo quedan fuera los movimientos de dinero del usuario (depósitos, crédito, bonos), que no son resultado.
        realized = sum(auto_rules.deal_net(d) for d in raw if d["type"] not in DEAL_TYPES_BALANCE_OPS and d["time"] >= day_start)
        balance_ops = sum(d["profit"] for d in raw if d["type"] in DEAL_TYPES_BALANCE_OPS and d["time"] >= day_start)
        floating = sum(p["profit"] + p["swap"] for p in positions)
        state = RiskState(
            open_positions=len(positions),
            total_volume=round(sum(p["volume"] for p in positions), 4),
            realized_today=round(realized, 2),
            floating_pnl=round(floating, 2),
            balance_ops_today=round(balance_ops, 2),
            halted=self.halted(),
            trade_allowed=term["trade_allowed"],
            python_api_disabled=term["tradeapi_disabled"],
            account_trade_allowed=acct.trade_allowed,
            account_expert_allowed=acct.trade_expert,
            now_utc=now,
        )
        stats = auto_rules.compute_stats(self._stats_deals(trades, positions, day_start), positions, self.cfg.auto_magic,
                                         day_start, self._auto_ids())
        return Snapshot(now=now, acct=acct, term=term, positions=positions, day_start=day_start, state=state, auto=stats,
                        server_now=self.clock.server_now(utc_ts), clock_blocker=self._clock_blocker(utc_ts))

    def _state(self) -> RiskState:
        return self._snapshot().state

    def _market_request(self, spec, side: str, volume: float, price: float, sl: float, tp: float | None, comment: str, magic: int) -> dict:
        m = self._mt5()
        return {
            "action": m.TRADE_ACTION_DEAL,
            "symbol": spec.name,
            "volume": float(volume),
            "type": m.ORDER_TYPE_BUY if side == SIDE_BUY else m.ORDER_TYPE_SELL,
            "price": float(price),
            "sl": normalize_price(spec, sl),
            "tp": normalize_price(spec, tp) if tp else 0.0,
            "deviation": int(self.cfg.max_slippage_points),
            "magic": int(magic),
            "comment": (comment or "claude")[:31],
            "type_time": m.ORDER_TIME_GTC,
            "type_filling": pick_filling(spec),
        }

    def _prepare(self, symbol: str, side: str, sl: float, tp: float | None, volume: float | None, comment: str, risk_pct: float | None, magic: int | None = None):
        side = normalize_side(side)
        spec = self.b.symbol(symbol)
        # SL y TP se llevan a la rejilla de ticks del símbolo ANTES de dimensionar: el volumen, las comprobaciones, el
        # resumen y la orden usan así el mismo precio, y el riesgo real en el stop nunca supera el presupuesto.
        if is_finite_positive(sl):
            sl = normalize_price(spec, float(sl))
        if tp is not None and is_finite_positive(tp):
            tp = normalize_price(spec, float(tp))
        snap = self._snapshot([spec.name])
        tick = self.b.tick(spec.name)  # después de la foto: si hubo que esperar al reloj, el precio es el de ahora
        intent = OrderIntent(symbol=spec.name, side=side, sl=sl, tp=tp, volume=volume, comment=comment, risk_pct=risk_pct)
        ev = evaluate_order(self.cfg, snap.acct, spec, snap.state, intent, tick["bid"], tick["ask"])
        if snap.clock_blocker:
            # sin hora del servidor no se sabe qué operaciones son de hoy: el límite de pérdida diaria no es fiable
            ev.reasons.append(snap.clock_blocker)
            ev.ok = False
        req = None
        if ev.ok and ev.volume > 0:
            req = self._market_request(spec, side, ev.volume, ev.entry_price, ev.sl, ev.tp, comment, self.cfg.magic if magic is None else magic)
        return snap, spec, tick, intent, ev, req

    @staticmethod
    def _summary(acct, spec, side, ev: Evaluation) -> dict:
        return {
            "account": acct.login,
            "mode": acct.mode_name,
            "symbol": spec.name,
            "side": side,
            "price": ev.entry_price,
            "volume": ev.volume,
            "sl": ev.sl,
            "tp": ev.tp,
            "sl_points": ev.sl_points,
            "risk_amount": round(ev.risk_amount, 2),
            "risk_pct": round(ev.risk_pct, 3),
            "currency": acct.currency,
            "spread_points": spec.spread,
        }

    # ---- lecturas ------------------------------------------------------------------------------
    def _account_info(self, snap: Snapshot) -> dict:
        acct, term = snap.acct, snap.term
        return {
            "login": acct.login,
            "mode": acct.mode_name,
            "is_demo": acct.is_demo,
            "server": acct.server,
            "currency": acct.currency,
            "balance": acct.balance,
            "equity": acct.equity,
            "margin_free": acct.margin_free,
            "leverage": acct.leverage,
            "terminal_connected": term["connected"],
            "algo_trading_enabled": term["trade_allowed"],
            "python_api_trading_disabled": term["tradeapi_disabled"],
            "account_trade_allowed": acct.trade_allowed,
            "account_expert_allowed": acct.trade_expert,
            "halted": snap.state.halted,
            "live_trading_allowed_by_config": self.cfg.allow_live,
            "auto_mode_enabled": self.cfg.auto.enabled,
            "warning": None if acct.is_demo else "CUENTA REAL: cada orden compromete dinero real",
        }

    def account_info(self) -> dict:
        acct = self.b.account()
        term = self.b.terminal()
        snap = Snapshot(now=self._now(), acct=acct, term=term, positions=[], day_start=0, state=RiskState(halted=self.halted()), auto=auto_rules.AutoStats())
        return self._account_info(snap)

    def _risk_status(self, snap: Snapshot) -> dict:
        acct, st = snap.acct, snap.state
        limit = daily_loss_limit(self.cfg, acct, st)
        today = pnl_today(st)
        blockers = trading_blockers(self.cfg, acct, st)
        if snap.clock_blocker:
            blockers = [*blockers, snap.clock_blocker]
        return {
            "mode": acct.mode_name,
            "halted": st.halted,
            "algo_trading_enabled": st.trade_allowed,
            "python_api_trading_disabled": st.python_api_disabled,
            "can_open_new_positions": not blockers,
            "blockers": blockers,
            "note": "can_open_new_positions resume solo los bloqueos globales; el veredicto por orden (símbolo, volumen total, spread) lo da order_check",
            "pnl_today": today,
            "realized_today": st.realized_today,
            "floating_pnl": st.floating_pnl,
            "balance_ops_today": st.balance_ops_today,
            "day_window": DAY_UNVERIFIED if snap.clock_blocker else DAY_UTC,
            "daily_loss_limit": round(limit, 2),
            "daily_loss_hit": daily_loss_hit(self.cfg, acct, st),
            "remaining_before_daily_halt": round(today - limit, 2),
            "open_positions": st.open_positions,
            "max_open_positions": self.cfg.max_open_positions,
            "total_volume": st.total_volume,
            "max_total_volume": self.cfg.max_total_volume,
            "limits": {
                "allow_live": self.cfg.allow_live,
                "max_risk_pct_per_trade": self.cfg.max_risk_pct_per_trade,
                "max_daily_loss_pct": self.cfg.max_daily_loss_pct,
                "max_volume_per_order": self.cfg.max_volume_per_order,
                "symbols": list(self.cfg.symbols) or "cualquiera",
                "require_tp": self.cfg.require_tp,
                "trading_hours_utc": self.cfg.trading_hours_utc,
                "max_spread_points": self.cfg.max_spread_points,
            },
            "currency": acct.currency,
            **self._clock_info(),
        }

    def risk_status(self) -> dict:
        return self._risk_status(self._snapshot())

    def symbol_info(self, symbol: str) -> dict:
        spec = self.b.symbol(symbol)
        d = spec.__dict__.copy()
        d["allowed_by_config"] = symbol_allowed(self.cfg, spec.name)
        d["filling_chosen"] = pick_filling(spec)
        return d

    def _quote(self, spec) -> dict:
        self._sync_clock([spec.name], self._now().timestamp())
        t = self.b.tick(spec.name)  # después de sincronizar: si hubo que esperar al reloj, la cotización es la de ahora
        age = self.clock.age(t["time"], self._now().timestamp())
        spread_points = round((t["ask"] - t["bid"]) / spec.point, 1) if spec.point > 0 else None
        return {
            "symbol": spec.name,
            "bid": t["bid"],
            "ask": t["ask"],
            "spread_points": spread_points,
            "time": t["time"],
            "age_seconds": None if age is None else int(age),
            "stale": age is None or age > STALE_SECONDS,
            "digits": spec.digits,
        }

    def quote(self, symbol: str) -> dict:
        return self._quote(self.b.symbol(symbol))

    def bars(self, symbol: str, timeframe: str = "M15", count: int = 30) -> dict:
        """Velas cerradas con indicadores calculados por el servidor (nunca por el modelo)."""
        count = max(5, min(int(count), 200))
        tf = normalize_timeframe(timeframe)
        spec = self.b.symbol(symbol)
        q = self._quote(spec)  # también sincroniza la hora del servidor, antes de leer las velas
        rows = self.b.rates(spec.name, tf, max(count, WARMUP_BARS) + 1)
        if len(rows) < 3:
            raise BrokerError(f"no hay velas suficientes de {spec.name} en {tf}")
        # La última fila solo está en formación si su periodo no ha terminado: MT5 no crea la vela nueva hasta que
        # llega su primer tick, así que con el mercado parado la última fila es una vela ya cerrada. Se decide por
        # tiempo. Sin hora del servidor se usa una cota inferior segura: ningún servidor va más de 12 h por detrás
        # de UTC y ningún tick viene del futuro.
        utc_ts = self._now().timestamp()
        server_now = self.clock.server_now(utc_ts)
        newest_tick = max(q["time"], *self.clock.prev.values())  # ningún tick viene del futuro: prueba de que ese instante ya pasó
        # Con hora conocida, el reloj de este PC puede ir hasta MAX_BEHIND s adelantado sin que se note: en ese margen
        # solo un tick posterior al fin de la vela prueba que cerró.
        floor = max(newest_tick, utc_ts + MIN_OFFSET if server_now is None else server_now - MAX_BEHIND)
        last_is_closed = rows[-1]["time"] + TF_SECONDS[tf] <= floor
        closed = rows if last_is_closed else rows[:-1]
        forming = None if last_is_closed else rows[-1]
        may_be_closed = forming is not None and (server_now is None or rows[-1]["time"] + TF_SECONDS[tf] <= server_now)
        closes = [r["close"] for r in closed]
        highs = [r["high"] for r in closed]
        lows = [r["low"] for r in closed]
        nd = spec.digits + 1

        def rnd(x: float | None) -> float | None:
            return None if x is None else round(x, nd)

        ema20, ema50 = ema(closes, 20), ema(closes, 50)
        rsi14, atr14 = rsi(closes, 14), atr(highs, lows, closes, 14)
        last = closes[-1]
        trend = "unknown"
        if ema20 is not None and ema50 is not None:
            # Con los mismos valores redondeados que se devuelven: sin redondear, en una serie plana el último bit de
            # cada media decide un "up" o "down" que la respuesta (ema20 == ema50 == last_close) no deja ver.
            fast, slow, price = rnd(ema20), rnd(ema50), rnd(last)
            if fast > slow and price > slow:
                trend = "up"
            elif fast < slow and price < slow:
                trend = "down"
            else:
                trend = "flat"
        lookback = min(20, len(closed))

        def stamp(t: int) -> str:
            return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

        def bar(r: dict) -> dict:
            return {"time_server": stamp(r["time"]), "open": rnd(r["open"]), "high": rnd(r["high"]), "low": rnd(r["low"]), "close": rnd(r["close"])}

        note = "indicadores calculados sobre velas cerradas; las horas son del reloj del servidor del broker"
        if may_be_closed:
            note += (". AVISO: " + ("hora del servidor desconocida" if server_now is None else "ninguna cotización desde antes del cierre de la vela")
                     + "; la vela de forming_bar puede estar ya cerrada (con el mercado parado lo está) y los indicadores no la incluyen")
        return {
            "symbol": spec.name,
            "timeframe": tf,
            "closed_bars_used": len(closed),
            "quote": q,
            "indicators": {
                "ema20": rnd(ema20),
                "ema50": rnd(ema50),
                "rsi14": None if rsi14 is None else round(rsi14, 1),
                "atr14": rnd(atr14),
                "atr14_points": None if atr14 is None or spec.point <= 0 else round(atr14 / spec.point, 1),
                "trend": trend,
                # + 0.0: un resto negativo ínfimo no debe salir como "-0.0"
                "close_minus_ema20_in_atr": None if atr14 in (None, 0) or ema20 is None else round((last - ema20) / atr14, 2) + 0.0,
                "highest_high_20": rnd(max(highs[-lookback:])),
                "lowest_low_20": rnd(min(lows[-lookback:])),
                "last_close": rnd(last),
            },
            "last_closed_bar": bar(closed[-1]),
            "forming_bar": None if forming is None else bar(forming),
            "forming_bar_may_be_closed": may_be_closed,
            "bars": [[stamp(r["time"]), rnd(r["open"]), rnd(r["high"]), rnd(r["low"]), rnd(r["close"])] for r in closed[-count:]],
            "bars_format": "[hora del servidor, open, high, low, close], de la más antigua a la más reciente; solo velas cerradas",
            "note": note,
            **self._clock_info(),
        }

    def positions(self, symbol: str | None = None) -> list[dict]:
        out = self.b.positions(symbol)
        for p in out:
            p["opened_by"] = self._owner(p)
        return out

    def pending_orders(self) -> list[dict]:
        return self.b.pending_orders()

    def history_today(self) -> dict:
        snap = self._snapshot()
        raw = self.b.deals(snap.day_start, snap.day_start + 2 * 86400)
        deals = [d for d in raw if d["type"] in (DEAL_TYPE_BUY, DEAL_TYPE_SELL)]
        charges = [d for d in raw if d["type"] not in (DEAL_TYPE_BUY, DEAL_TYPE_SELL) and d["type"] not in DEAL_TYPES_BALANCE_OPS]
        return {
            "deals": deals,
            "count": len(deals),
            "realized_pnl": snap.state.realized_today,
            "broker_charges": round(sum(auto_rules.deal_net(d) for d in charges), 2),
            "balance_ops": snap.state.balance_ops_today,
            "day_window": DAY_UNVERIFIED if snap.clock_blocker else DAY_UTC,
            "note": f"{DAY_UNVERIFIED if snap.clock_blocker else DAY_UTC}; realized_pnl incluye operaciones y cargos del broker; "
                    "los tiempos de los deals están en el reloj del servidor",
            **self._clock_info(),
        }

    def journal_tail(self, n: int = 20) -> list[dict]:
        return self.journal.tail(n)

    # ---- previsualización --------------------------------------------------------------------
    def position_size(self, symbol: str, side: str, sl: float, risk_pct: float | None = None) -> dict:
        side = normalize_side(side)
        acct = self.b.account()
        spec = self.b.symbol(symbol)
        tick = self.b.tick(spec.name)
        if not is_finite_positive(sl):
            return {"error": "sl debe ser un número > 0"}
        sl = normalize_price(spec, float(sl))  # en la rejilla de ticks, como lo usarán order_check y order_send
        if sl <= 0:
            return {"error": "sl debe ser un número > 0"}
        entry = tick["ask"] if side == SIDE_BUY else tick["bid"]
        close_ref = tick["bid"] if side == SIDE_BUY else tick["ask"]
        problems = stops_ok(side, close_ref, sl, None)
        if problems:
            return {"error": "; ".join(problems), "price": entry}
        try:
            vol, risk_amount, lpl = size_for_risk(self.cfg, spec, acct.equity, entry, sl, risk_pct)
        except RiskError as exc:
            return {"error": str(exc), "price": entry}
        return {
            "symbol": spec.name,
            "side": side,
            "price": entry,
            "sl": sl,
            "sl_points": round(abs(entry - sl) / spec.point, 1) if spec.point else None,
            "volume": vol,
            "risk_budget": round(risk_amount, 2),
            "actual_risk": round(vol * lpl, 2),
            "loss_per_lot": round(lpl, 2),
            "currency": acct.currency,
            "equity": acct.equity,
            "note": "volumen ya acotado por max_risk_pct_per_trade, max_volume_per_order y volume_max del broker",
        }

    def order_check(self, symbol: str, side: str, sl: float, tp: float | None = None, volume: float | None = None, comment: str = "", risk_pct: float | None = None) -> dict:
        snap, spec, tick, intent, ev, req = self._prepare(symbol, side, sl, tp, volume, comment, risk_pct)
        out = {"would_send": ev.ok, "evaluation": ev.as_dict(), "summary": self._summary(snap.acct, spec, intent.side, ev)}
        if ev.ok and req is not None:
            out["broker_check"] = self.b.order_check(req)
            out["would_send"] = int(out["broker_check"].get("retcode", -1)) == 0
            out["request"] = req
        self.journal.record("order_check", summary=out["summary"], ok=out["would_send"], reasons=ev.reasons)
        return out

    # ---- escrituras con confirmación humana ----------------------------------------------------
    def _send_market(self, req: dict, summary: dict, ev: Evaluation, events: dict[str, str], **extra: Any) -> dict:
        """order_check del broker y envío de una APERTURA. `events` = nombres de los eventos del journal.

        Antes de enviar se anota el intento; si esa escritura falla no se envía nada (una apertura sin rastro es peor
        que una apertura sin enviar). Después del envío el journal es de mejor esfuerzo: pase lo que pase se devuelve
        el resultado real de la orden.
        """
        check = self.b.order_check(req)
        if int(check.get("retcode", -1)) != 0:
            out = {"sent": False, "status": "rejected", "error": f"order_check rechazó la orden: {check.get('retcode_name')} {check.get('comment', '')}", "broker_check": check, "summary": summary}
            return self._with_journal(out, events["check"], summary=summary, check=check, **extra)
        if self.halted():
            # El kill switch se miró al tomar la foto; desde entonces han pasado la lectura del precio y el order_check
            # del broker. Si en ese rato alguien paró el trading (el panel, kill_switch.py, halt_trading), no se abre.
            out = {"sent": False, "status": "rejected", "error": HALT_BEFORE_SEND_MSG.format(halt_file=self.halt_path), "summary": summary}
            return self._with_journal(out, events["halt"], summary=summary, **extra)
        try:
            self.journal.record(events["attempt"], summary=summary, request=req, **extra)
        except Exception as exc:
            problem = f"{type(exc).__name__}: {exc}"
            return {"sent": False, "status": "rejected", "error": f"Orden NO enviada: no se pudo anotar el intento en el journal ({problem}); sin registro no se abre nada",
                    "journal_error": problem, "summary": summary}
        try:
            result = self.b.order_send(req)
        except BrokerError as exc:
            out = {
                "sent": False,
                "status": "unknown",
                "error": UNKNOWN_RESULT_MSG.format(exc=exc),
                "partial": False,
                "retcode": None,
                "retcode_name": NO_RESPONSE,
                "order": None,
                "deal": None,
                "fill_price": None,
                "fill_volume": None,
                "requested_volume": ev.volume,
                "comment": str(exc),
                "summary": summary,
                "warnings": ev.warnings,
                "next": "resultado desconocido: verifica con positions() si la posición existe; no envíes otra orden",
            }
            return self._with_journal(out, events["unknown"], summary=summary, request=req, error=str(exc), **extra)
        code = int(result.get("retcode", -1))
        status = _status(code)
        ok = status != "failed"
        out = {
            "sent": ok,
            "status": status,
            "partial": status == "partial",
            "retcode": code,
            "retcode_name": retcode_name(code),
            "order": result.get("order"),
            "deal": result.get("deal"),
            "fill_price": result.get("price"),
            "fill_volume": result.get("volume"),
            "requested_volume": ev.volume,
            "comment": result.get("comment"),
            "summary": summary,
            "warnings": ev.warnings,
            "next": "no reintentes automáticamente; informa al usuario" if not ok else "verifica con positions(); si status es partial o placed, NO envíes otra orden por el resto sin volver a confirmar",
        }
        return self._with_journal(out, events["sent"] if ok else events["failed"], status=status, summary=summary, request=req,
                                  result=result, warnings=ev.warnings, **extra)

    def order_send(self, symbol: str, side: str, sl: float, tp: float | None = None, volume: float | None = None, comment: str = "", confirm: bool = False, risk_pct: float | None = None) -> dict:
        snap, spec, tick, intent, ev, req = self._prepare(symbol, side, sl, tp, volume, comment, risk_pct)
        summary = self._summary(snap.acct, spec, intent.side, ev)
        if not ev.ok or req is None:
            self.journal.record("order_rejected_by_risk", summary=summary, reasons=ev.reasons)
            return {"sent": False, "status": "rejected", "error": "rechazada por el motor de riesgo", "evaluation": ev.as_dict(), "summary": summary}
        if not confirm:
            self.journal.record("order_pending_confirmation", summary=summary)
            return {"sent": False, "status": "needs_confirmation", "error": CONFIRM_MSG, "evaluation": ev.as_dict(), "summary": summary}
        return self._send_market(req, summary, ev, ORDER_EVENTS)

    def _find_position(self, ticket: int, positions: list[dict] | None = None) -> dict | None:
        ticket = int(ticket)
        for p in positions if positions is not None else self.b.positions():
            if p["ticket"] == ticket:
                return p
        return None

    def close_position(self, ticket: int, confirm: bool = False) -> dict:
        pos = self._find_position(ticket)
        if pos is None:
            return {"closed": False, "error": f"no existe una posición abierta con ticket {int(ticket)} (¿la cerró ya el SL/TP? mira history_today)"}
        if not confirm:
            self.journal.record("close_pending_confirmation", position=pos)
            return {"closed": False, "error": "Cierre NO enviado: falta confirm=true tras confirmación explícita del usuario", "position": pos}
        return self._close(pos)

    def _close(self, pos: dict, tag: str = "", magic: int | None = None, **extra: Any) -> dict:
        m = self._mt5()
        spec = self.b.symbol(pos["symbol"])
        tick = self.b.tick(spec.name)
        closing_buy = pos["side"] == SIDE_BUY
        req = {
            "action": m.TRADE_ACTION_DEAL,
            "position": int(pos["ticket"]),
            "symbol": spec.name,
            "volume": float(pos["volume"]),
            "type": m.ORDER_TYPE_SELL if closing_buy else m.ORDER_TYPE_BUY,
            "price": tick["bid"] if closing_buy else tick["ask"],
            "deviation": int(self.cfg.max_slippage_points),
            "magic": int(self.cfg.magic if magic is None else magic),
            "comment": "claude-auto close" if tag else "claude close",
            "type_time": m.ORDER_TIME_GTC,
            "type_filling": pick_filling(spec),
        }
        # Cerrar reduce riesgo: nunca depende del journal. Se envía sin anotar antes y el registro posterior es de
        # mejor esfuerzo (si falla, el resultado real se devuelve igual, con journal_error).
        try:
            result = self.b.order_send(req)
        except BrokerError as exc:
            out = {
                "closed": False,
                "status": "unknown",
                "partial": False,
                "ticket": pos["ticket"],
                "retcode": None,
                "retcode_name": NO_RESPONSE,
                "requested_volume": pos["volume"],
                "fill_volume": None,
                "fill_price": None,
                "comment": str(exc),
                "error": UNKNOWN_RESULT_MSG.format(exc=exc),
                "next": "resultado desconocido: verifica con positions() si la posición sigue abierta y no reintentes por tu cuenta",
            }
            return self._with_journal(out, tag + "close_unknown", position=pos, request=req, error=str(exc), **extra)
        code = int(result.get("retcode", -1))
        status = _status(code)
        closed = status == "filled"
        event = {"filled": "position_closed", "partial": "close_partial", "placed": "close_placed"}.get(status, "close_failed")
        out = {
            "closed": closed,
            "status": status,
            "partial": status == "partial",
            "ticket": pos["ticket"],
            "retcode": code,
            "retcode_name": retcode_name(code),
            "requested_volume": pos["volume"],
            "fill_volume": result.get("volume"),
            "fill_price": result.get("price"),
            "comment": result.get("comment"),
            "next": None if closed else "la posición puede seguir abierta (total o parcialmente): verifica con positions() y no reintentes por tu cuenta",
        }
        return self._with_journal(out, tag + event, position=pos, request=req, result=result, **extra)

    def _send_sltp(self, pos: dict, spec, new_sl: float, new_tp: float, magic: int, events: tuple[str, str, str], **extra: Any) -> dict:
        """Cambia SL/TP. `events` = (modificado, rechazado, sin respuesta). Como al cerrar, no depende del journal."""
        m = self._mt5()
        req = {"action": m.TRADE_ACTION_SLTP, "position": int(pos["ticket"]), "symbol": spec.name, "sl": new_sl, "tp": new_tp, "magic": int(magic)}
        try:
            result = self.b.order_send(req)
        except BrokerError as exc:
            out = {"modified": False, "status": "unknown", "ticket": pos["ticket"], "retcode": None, "retcode_name": NO_RESPONSE, "sl": new_sl, "tp": new_tp,
                   "comment": str(exc), "error": UNKNOWN_RESULT_MSG.format(exc=exc)}
            return self._with_journal(out, events[2], position=pos, request=req, error=str(exc), **extra)
        code = int(result.get("retcode", -1))
        ok = code == RETCODE_DONE
        out = {"modified": ok, "ticket": pos["ticket"], "retcode": code, "retcode_name": retcode_name(code), "sl": new_sl, "tp": new_tp, "comment": result.get("comment")}
        return self._with_journal(out, events[0] if ok else events[1], position=pos, request=req, result=result, **extra)

    def modify_position(self, ticket: int, sl: float | None = None, tp: float | None = None, confirm: bool = False) -> dict:
        pos = self._find_position(ticket)
        if pos is None:
            return {"modified": False, "error": f"no existe una posición abierta con ticket {int(ticket)} (¿la cerró ya el SL/TP? mira history_today)"}
        if sl is not None and not is_finite_positive(sl):
            return {"modified": False, "error": "no se permite quitar el stop loss (sl debe ser un número > 0)", "position": pos}
        if tp is not None and tp != 0 and not is_finite_positive(tp):
            return {"modified": False, "error": "tp inválido (número > 0, o 0 para quitarlo)", "position": pos}
        spec = self.b.symbol(pos["symbol"])
        new_sl = normalize_price(spec, float(sl)) if sl is not None else float(pos["sl"])
        new_tp = normalize_price(spec, float(tp)) if tp else (float(pos["tp"]) if tp is None else 0.0)
        if new_sl <= 0:
            return {"modified": False, "error": "no se permite quitar el stop loss (sl debe ser > 0)", "position": pos}
        # price_current de una posición es el precio que la cierra: bid para compras, ask para ventas
        close_ref = pos["price_current"] or pos["price_open"]
        problems = stops_ok(pos["side"], close_ref, new_sl, new_tp if new_tp > 0 else None)
        if problems:
            return {"modified": False, "error": "; ".join(problems), "position": pos}
        if not confirm:
            return {"modified": False, "error": "Modificación NO enviada: falta confirm=true tras confirmación explícita del usuario", "position": pos, "proposed": {"sl": new_sl, "tp": new_tp}}
        return self._send_sltp(pos, spec, new_sl, new_tp, self.cfg.magic, ("position_modified", "modify_failed", "modify_unknown"))

    def halt_trading(self, reason: str = "", close_all: bool = False, confirm: bool = False) -> dict:
        # Lo primero es el bloqueo, antes de cualquier llamada a MT5 o al journal: en memoria (no puede fallar) y en
        # el archivo HALT, que es lo que ven el bucle automático y los demás procesos.
        self._halt_latched = True
        closed: list[dict] = []
        errors: list[str] = []
        try:
            self.halt_path.parent.mkdir(parents=True, exist_ok=True)
            stamp = self._now().isoformat(timespec="seconds")
            with self.halt_path.open("a", encoding="utf-8") as fh:
                fh.write(f"{stamp} {reason or 'halt solicitado'}\n")
        except OSError as exc:
            if not self.halt_path.exists():
                if not self._latch_halt:
                    raise  # este proceso no enclava la parada (panel): sin archivo no hay nada detenido
                errors.append(
                    f"no se pudo crear el archivo {self.halt_path} ({exc}): este servidor queda bloqueado hasta reiniciarlo, "
                    "pero el bucle automático y otros procesos NO se detienen; crea el archivo a mano"
                )
        result = {
            "halted": True,
            "halt_file": str(self.halt_path),
            "closed": closed,
            "errors": errors,
            "resume": f"para reanudar, un humano debe borrar el archivo {self.halt_path} y reiniciar el servidor MCP",
        }
        if close_all and not confirm:
            errors.append("close_all NO ejecutado: cerrar todas las posiciones a mercado exige confirm=true tras confirmación explícita del usuario")
            return self._with_journal(result, "halt", reason=reason, close_all=False, note="close_all pedido sin confirm=true; solo se bloqueó")
        if close_all:
            try:
                positions = self.b.positions()
            except BrokerError as exc:
                positions = []
                errors.append(f"no se pudieron leer las posiciones para cerrarlas: {exc}")
            for pos in positions:
                try:
                    res = self._close(pos)
                    closed.append(res)
                    if not res["closed"]:
                        errors.append(f"{pos['ticket']}: {res['retcode_name']} ({res['status']})")
                except Exception as exc:  # un fallo con una posición no impide intentar las demás
                    errors.append(f"{pos['ticket']}: {exc}")
        # El archivo HALT ya existe: un fallo del journal no convierte la parada en un error.
        return self._with_journal(result, "halt", reason=reason, close_all=close_all, closed=closed, errors=errors)

    # ---- modo automático (solo si el usuario lo activó en la configuración) ----------------------
    def _auto_status(self, snap: Snapshot) -> dict:
        a = self.cfg.auto
        st = snap.state
        mode = auto_rules.mode_blockers(self.cfg, snap.acct, st.halted)
        risk = [r for r in trading_blockers(self.cfg, snap.acct, st) if "kill switch" not in r and "allow_live=false" not in r]
        if st.total_volume >= self.cfg.max_total_volume - 1e-12:
            risk.append(f"volumen total abierto {st.total_volume} ya alcanza max_total_volume={self.cfg.max_total_volume}")
        # Bloqueos de entrada (no de modo): con ellos el bot no abre, pero sigue gestionando sus posiciones.
        entry: list[str] = []
        if a.enabled and not self.cfg.symbols:
            entry.append(SYMBOLS_EMPTY_MSG)
        elif a.enabled and all(self._resolved.get(name) is None for name in self.cfg.symbols):
            entry.append(SYMBOLS_UNKNOWN_MSG)
        if snap.server_now is None:
            # Sin hora del servidor, el cupo, la racha y el enfriamiento no se pueden comprobar: no se presentan
            # bloqueos calculados con una hora supuesta, solo el motivo real.
            entry.append(snap.clock_blocker or "hora del servidor desconocida")
            cooldown = None
        else:
            entry += auto_rules.global_entry_blockers(self.cfg, snap.auto, snap.server_now)
            # hacia arriba, como el texto del bloqueo: mientras quede espera nunca vale 0
            cooldown = math.ceil(auto_rules.cooldown_remaining(self.cfg, snap.auto, snap.server_now) * 10 - 1e-9) / 10
            # Un proceso de larga vida (panel, sesión de Claude Code) sigue conociendo la hora horas después de cerrar
            # el mercado. Si ningún símbolo de la lista cotiza, auto_order rechazaría cualquier entrada por cotización
            # vieja: se dice también aquí, para que can_open no prometa lo que el servidor no va a aceptar.
            quiet = self._whitelist_quiet_seconds(snap.server_now)
            if a.enabled and quiet is not None and quiet > a.max_quote_age_seconds:
                listed = dict.fromkeys(exact for name in self.cfg.symbols if (exact := self._resolved.get(name)))
                moving = [exact for exact in listed
                          if self._just_arrived(exact, self.clock.age(self.clock.prev.get(exact, 0), snap.utc_ts), snap.utc_ts)]
                if moving:
                    # Se acaba de ver llegar su cotización y aun así parece vieja: no es un mercado parado. Lo normal es
                    # que el reloj de este PC vaya adelantado; la otra posibilidad es que el terminal acabe de reconectar.
                    entry.append(f"llegan cotizaciones de {', '.join(moving)}, pero este PC las ve con {_ago(quiet)} de retraso "
                                 f"(máximo {a.max_quote_age_seconds} s): su reloj va adelantado, o el terminal acaba de reconectar "
                                 "y son las últimas de un mercado parado; si sigue igual con el mercado abierto, sincroniza la "
                                 "hora de Windows")
                else:
                    entry.append(f"sin cotizaciones recientes en los símbolos de `symbols`: la última llegó hace {_ago(quiet)} "
                                 f"(máximo {a.max_quote_age_seconds} s); mercado parado o sin ticks")
        blockers = [*mode, *risk, *entry]
        limit = daily_loss_limit(self.cfg, snap.acct, st)
        today = pnl_today(st)
        return {
            "enabled": a.enabled,
            "can_open": not blockers,
            "can_manage_positions": not mode,
            "terminal_can_trade": st.can_trade,
            "blockers": blockers,
            "account_mode": snap.acct.mode_name,
            "trades_today": snap.auto.trades_today,
            "max_trades_per_day": a.max_trades_per_day,
            "consecutive_losses": snap.auto.consecutive_losses,
            "max_consecutive_losses": a.max_consecutive_losses,
            "closed_today": snap.auto.closed_today,
            "wins_today": snap.auto.wins_today,
            "realized_today_auto": snap.auto.realized_today,
            "cooldown_remaining_minutes": cooldown,
            "open_auto_positions": [
                {k: p[k] for k in ("ticket", "symbol", "side", "volume", "price_open", "price_current", "sl", "tp", "profit")}
                for p in snap.auto.open_positions
            ],
            "pnl_today": today,
            "day_window": DAY_UNVERIFIED if snap.server_now is None else DAY_UTC,
            "daily_loss_limit": round(limit, 2),
            "remaining_before_daily_halt": round(today - limit, 2),
            "limits": {
                "risk_pct_per_trade": self.cfg.auto_risk_pct,
                "min_rr": a.min_rr,
                "cooldown_minutes": a.cooldown_minutes,
                "one_position_per_symbol": a.one_position_per_symbol,
                "max_quote_age_seconds": a.max_quote_age_seconds,
                "allow_live": a.allow_live,
                "symbols": list(self.cfg.symbols) or "cualquiera",
                "trading_hours_utc": self.cfg.trading_hours_utc,
            },
            "currency": snap.acct.currency,
            **self._clock_info(),
        }

    def auto_status(self) -> dict:
        return self._auto_status(self._snapshot())

    def auto_order(self, symbol: str, side: str, sl: float, tp: float, reason: str) -> dict:
        """Orden de mercado SIN confirmación humana. Solo pasa si el modo automático está activado y todo cuadra."""
        a = self.cfg.auto
        reason = (reason or "").strip()
        snap, spec, tick, intent, ev, req = self._prepare(symbol, side, sl, tp, None, "claude-auto", self.cfg.auto_risk_pct, magic=self.cfg.auto_magic)
        mode = auto_rules.mode_blockers(self.cfg, snap.acct, snap.state.halted)
        reasons = [*mode, *[r for r in ev.reasons if not (mode and ("kill switch" in r or "allow_live=false" in r))]]
        if not self.cfg.symbols:
            reasons.append(SYMBOLS_EMPTY_MSG)
        if snap.server_now is not None:  # si no se conoce la hora del servidor, ev.reasons ya trae ese bloqueo
            reasons += auto_rules.global_entry_blockers(self.cfg, snap.auto, snap.server_now)
        utc_ts = self._now().timestamp()
        quote_age = self.clock.age(tick["time"], utc_ts)
        reasons += auto_rules.symbol_entry_blockers(self.cfg, spec.name, [p["symbol"] for p in snap.positions], quote_age,
                                                    just_arrived=self._just_arrived(spec.name, quote_age, utc_ts))
        if len(reason) < MIN_REASON_CHARS:
            reasons.append(REASON_MSG)
        if ev.tp is None:
            reasons.append("take profit obligatorio en modo automático")
        elif ev.entry_price > 0 and ev.sl > 0:
            rr = auto_rules.rr_ratio(ev.entry_price, ev.sl, ev.tp)
            if rr + 1e-9 < a.min_rr:
                reasons.append(f"ratio beneficio/riesgo {rr:.2f} menor que el mínimo {a.min_rr}")
        if spec.spread > 0 and ev.sl_points > 0 and spec.spread > ev.sl_points / 3:
            reasons.append(f"el spread ({spec.spread} puntos) supera un tercio de la distancia al SL ({ev.sl_points} puntos)")
        summary = self._summary(snap.acct, spec, intent.side, ev)
        if reasons or req is None:
            self.journal.record("auto_rejected", summary=summary, reasons=reasons, reason=reason)
            return {"sent": False, "status": "rejected", "reasons": reasons, "summary": summary, "next": "no insistas ni cambies los niveles para forzar la entrada; termina el ciclo"}
        out = self._send_market(req, summary, ev, AUTO_ORDER_EVENTS, reason=reason)
        if out.get("sent"):
            # el ticket de la orden de apertura es el identificador de la posición durante toda su vida
            problem = self._register_auto_position(out.get("order"))
            if problem:
                out["journal_error"] = "; ".join(x for x in (out.get("journal_error"), f"no se pudo anotar en el registro de posiciones del bot: {problem}") if x)
        if out.get("status") == "unknown":
            found, problem = self._claim_unanswered_position(spec.name, intent.side, snap.positions)
            out["position_found"] = found
            if problem:
                out["journal_error"] = "; ".join(x for x in (out.get("journal_error"), f"no se pudo anotar en el registro de posiciones del bot: {problem}") if x)
            seen = f"hay una posición nueva en {spec.name} (ticket {', '.join(map(str, found))}), anotada como del bot; " if found else ""
            out["next"] = f"resultado desconocido: {seen}verifica con positions() si la posición existe, anótalo y termina el ciclo; no abras otra operación"
        else:
            out["next"] = "termina el ciclo; no abras otra operación ni completes ejecuciones parciales"
        return out

    def _claim_unanswered_position(self, symbol: str, side: str, before: list[dict]) -> tuple[list[int], str | None]:
        """Tras un envío sin respuesta, busca la posición que pudo abrirse y la anota como del bot (mejor esfuerzo).

        Con un broker que borra el magic, el registro es lo único que une la posición con el bot: sin anotarla, el cupo
        diario y el enfriamiento no la contarían y el bot no podría gestionarla. Se toma por suya una posición del
        mismo símbolo y lado que no estaba en la foto previa al envío. Si el terminal tampoco responde ahora, no se
        anota nada. Devuelve los tickets encontrados y, si lo hubo, el error al escribir el registro.
        """
        known = {p["ticket"] for p in before}
        try:
            new = [p for p in self.b.positions() if p["ticket"] not in known and p["symbol"] == symbol and p["side"] == side]
        except BrokerError:
            return [], None
        problems = [x for x in (self._register_auto_position(p["identifier"]) for p in new) if x]
        return [p["ticket"] for p in new], (problems[0] if problems else None)

    def _auto_position(self, ticket: int, reason: str) -> tuple[dict | None, list[str], Snapshot]:
        """Posición del bot sobre la que se puede actuar, o las razones por las que no."""
        snap = self._snapshot()
        reasons = list(auto_rules.mode_blockers(self.cfg, snap.acct, snap.state.halted))
        pos = self._find_position(ticket, snap.positions)
        if pos is None:
            reasons.append(f"no existe una posición abierta con ticket {int(ticket)} (probablemente la cerró su SL/TP)")
        elif not auto_rules.is_bot_position(pos, self.cfg.auto_magic, self._auto_ids()):
            reasons.append(f"la posición {int(ticket)} no la abrió el modo automático: solo el usuario decide sobre ella")
            pos = None
        if len((reason or "").strip()) < MIN_REASON_CHARS:
            reasons.append(REASON_MSG)
        return (None if reasons else pos), reasons, snap

    def auto_close(self, ticket: int, reason: str) -> dict:
        pos, reasons, _ = self._auto_position(ticket, reason)
        if pos is None:
            self.journal.record("auto_close_rejected", ticket=int(ticket), reasons=reasons, reason=reason)
            return {"closed": False, "status": "rejected", "reasons": reasons}
        return self._close(pos, tag="auto_", magic=self.cfg.auto_magic, reason=reason.strip())

    def auto_tighten_stop(self, ticket: int, sl: float, reason: str) -> dict:
        """Mueve el SL de una posición del bot solo en la dirección que reduce el riesgo."""
        pos, reasons, _ = self._auto_position(ticket, reason)
        if pos is not None:
            if not is_finite_positive(sl):
                reasons.append("sl debe ser un número > 0")
            else:
                spec = self.b.symbol(pos["symbol"])
                new_sl = normalize_price(spec, float(sl))
                old = float(pos["sl"])
                close_ref = pos["price_current"] or pos["price_open"]
                if old <= 0:
                    reasons.append("la posición no tiene SL: no se puede comprobar que el cambio reduzca el riesgo")
                elif (pos["side"] == SIDE_BUY and new_sl <= old) or (pos["side"] != SIDE_BUY and new_sl >= old):
                    reasons.append(f"solo se permite acercar el stop a favor de la posición: SL actual {old}, propuesto {new_sl}")
                reasons += stops_ok(pos["side"], close_ref, new_sl, None)
                if not reasons and spec.stops_level > 0 and distance_points(spec, close_ref, new_sl) < spec.stops_level:
                    reasons.append(f"SL demasiado cerca del precio: mínimo {spec.stops_level} puntos")
                if not reasons:
                    return self._send_sltp(pos, spec, new_sl, float(pos["tp"]), self.cfg.auto_magic, ("auto_stop_tightened", "auto_modify_failed", "auto_modify_unknown"), reason=reason.strip())
        self.journal.record("auto_modify_rejected", ticket=int(ticket), reasons=reasons, reason=reason)
        return {"modified": False, "status": "rejected", "reasons": reasons}

    # ---- panel en vivo -------------------------------------------------------------------------
    def overview(self, journal_entries: int = 40) -> dict:
        """Todo lo que muestra el panel en vivo, a partir de una única foto del terminal. No escribe nada."""
        snap = self._snapshot()
        positions = [dict(p) for p in snap.positions]
        for p in positions:
            p["opened_by"] = self._owner(p)
        return {
            "generated_at_utc": snap.now.isoformat(timespec="seconds"),
            "account": self._account_info(snap),
            "risk": self._risk_status(snap),
            "auto": self._auto_status(snap),
            "positions": positions,
            "journal": self.journal.tail(journal_entries),
        }
