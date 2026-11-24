"""Lógica de las herramientas MCP (independiente del transporte, para poder probarla)."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import auto as auto_rules
from .broker import DEAL_TYPE_BUY, DEAL_TYPE_SELL, DEAL_TYPES_BALANCE_OPS, BrokerError, MT5Broker, retcode_name
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
from .timeutil import ServerClock

RETCODE_DONE = 10009
RETCODE_PLACED = 10008
RETCODE_DONE_PARTIAL = 10010

LOOKBACK_DAYS = 30  # histórico que se lee para atribuir al bot posiciones abiertas en días anteriores
AUTO_IDS_FILE = "auto_positions.jsonl"  # registro propio de las posiciones que abre el bot (junto al journal)
AUTO_IDS_KEEP_DAYS = 120
STALE_SECONDS = 300  # una cotización más vieja que esto se marca como parada
CLOCK_WAIT_SECONDS = 6.0  # espera máxima a que un tick se mueva para fijar la hora del servidor
WARMUP_BARS = 260  # velas que se leen para que EMA/RSI/ATR estén estabilizados
MIN_REASON_CHARS = 15

CONFIRM_MSG = (
    "Orden NO enviada: falta confirm=true. Muestra al usuario el resumen (símbolo, lado, volumen, SL, TP, "
    "riesgo en divisa y %) y llama de nuevo con confirm=true y el mismo volume SOLO si el usuario confirma explícitamente."
)
REASON_MSG = (
    f"falta el motivo (reason): al menos {MIN_REASON_CHARS} caracteres con la regla de la estrategia que se cumple"
)


def _status(code: int) -> str:
    return {RETCODE_DONE: "filled", RETCODE_DONE_PARTIAL: "partial", RETCODE_PLACED: "placed"}.get(code, "failed")


@dataclass
class Snapshot:
    """Foto coherente del terminal para una llamada: una sola lectura de cuenta, posiciones e histórico."""

    now: datetime
    acct: AccountSnapshot
    term: dict
    positions: list[dict]
    day_start: int  # inicio del día UTC expresado en el reloj del servidor
    state: RiskState
    auto: auto_rules.AutoStats

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
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                if float(rec.get("ts", 0)) >= cutoff and int(rec["id"]) > 0:
                    ids.add(int(rec["id"]))
            except (ValueError, KeyError, TypeError):
                continue
        self._auto_ids_cache = (mtime, frozenset(ids))
        return self._auto_ids_cache[1]

    def _register_auto_position(self, position_id: Any) -> None:
        try:
            pid = int(position_id or 0)
        except (TypeError, ValueError):
            return
        if pid <= 0:
            return
        path = self.auto_ids_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": pid, "ts": round(self._now().timestamp(), 1)}) + "\n")
        self._auto_ids_cache = (-1.0, frozenset())

    def _owner(self, position: dict) -> str:
        if auto_rules.is_bot_position(position, self.cfg.auto_magic, self._auto_ids()):
            return "auto"
        return "manual" if position.get("magic") == self.cfg.magic else "externo"

    def _mt5(self) -> Any:
        return self.b.mt5

    def _ref_tick_times(self, extra: list[str]) -> list[int]:
        """Tiempos del último tick de los símbolos de referencia (lista blanca + los indicados)."""
        times: list[int] = []
        for name in dict.fromkeys([*self.cfg.symbols, *extra]):
            if name not in self._resolved:
                try:
                    self._resolved[name] = self.b.symbol(name).name
                except BrokerError:
                    self._resolved[name] = None  # no existe en este broker: no se reintenta en cada llamada
            exact = self._resolved[name]
            if exact is None:
                continue
            try:
                times.append(self.b.tick(exact)["time"])
            except BrokerError:
                continue
        return times

    def _sync_clock(self, extra: list[str], utc_ts: float) -> None:
        self.clock.update(self._ref_tick_times(extra), utc_ts)

    def _snapshot(self, extra_symbols: list[str] | None = None) -> Snapshot:
        now = self._now()
        utc_ts = now.timestamp()
        acct = self.b.account()
        term = self.b.terminal()
        positions = self.b.positions()
        self._sync_clock([*(extra_symbols or []), *(p["symbol"] for p in positions)], utc_ts)
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
        stats = auto_rules.compute_stats(trades, positions, self.cfg.auto_magic, day_start, self._auto_ids())
        return Snapshot(now=now, acct=acct, term=term, positions=positions, day_start=day_start, state=state, auto=stats)

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
        tick = self.b.tick(spec.name)
        snap = self._snapshot([spec.name])
        intent = OrderIntent(symbol=spec.name, side=side, sl=sl, tp=tp, volume=volume, comment=comment, risk_pct=risk_pct)
        ev = evaluate_order(self.cfg, snap.acct, spec, snap.state, intent, tick["bid"], tick["ask"])
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

    def _clock_info(self) -> dict:
        return {
            "server_clock_known": self.clock.known,
            "server_utc_offset_hours": None if self.clock.offset is None else self.clock.offset / 3600.0,
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
        t = self.b.tick(spec.name)
        utc_ts = self._now().timestamp()
        self._sync_clock([spec.name], utc_ts)
        age = self.clock.age(t["time"], utc_ts)
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
        spec = self.b.symbol(symbol)
        rows = self.b.rates(spec.name, timeframe, max(count, WARMUP_BARS) + 1)
        if len(rows) < 3:
            raise BrokerError(f"no hay velas suficientes de {spec.name} en {timeframe}")
        forming, closed = rows[-1], rows[:-1]
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
            if ema20 > ema50 and last > ema50:
                trend = "up"
            elif ema20 < ema50 and last < ema50:
                trend = "down"
            else:
                trend = "flat"
        lookback = min(20, len(closed))

        def stamp(t: int) -> str:
            return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

        def bar(r: dict) -> dict:
            return {"time_server": stamp(r["time"]), "open": rnd(r["open"]), "high": rnd(r["high"]), "low": rnd(r["low"]), "close": rnd(r["close"])}

        q = self._quote(spec)
        return {
            "symbol": spec.name,
            "timeframe": timeframe.upper(),
            "closed_bars_used": len(closed),
            "quote": q,
            "indicators": {
                "ema20": rnd(ema20),
                "ema50": rnd(ema50),
                "rsi14": None if rsi14 is None else round(rsi14, 1),
                "atr14": rnd(atr14),
                "atr14_points": None if atr14 is None or spec.point <= 0 else round(atr14 / spec.point, 1),
                "trend": trend,
                "close_minus_ema20_in_atr": None if atr14 in (None, 0) or ema20 is None else round((last - ema20) / atr14, 2),
                "highest_high_20": rnd(max(highs[-lookback:])),
                "lowest_low_20": rnd(min(lows[-lookback:])),
                "last_close": rnd(last),
            },
            "last_closed_bar": bar(closed[-1]),
            "forming_bar": bar(forming),
            "bars": [[stamp(r["time"]), rnd(r["open"]), rnd(r["high"]), rnd(r["low"]), rnd(r["close"])] for r in closed[-count:]],
            "bars_format": "[hora del servidor, open, high, low, close], de la más antigua a la más reciente; solo velas cerradas",
            "note": "indicadores calculados sobre velas cerradas; las horas son del reloj del servidor del broker",
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
        utc_ts = snap.utc_ts
        offset = self.clock.offset or 0
        raw = self.b.deals(snap.day_start, int(utc_ts + offset) + 86400)
        deals = [d for d in raw if d["type"] in (DEAL_TYPE_BUY, DEAL_TYPE_SELL)]
        charges = [d for d in raw if d["type"] not in (DEAL_TYPE_BUY, DEAL_TYPE_SELL) and d["type"] not in DEAL_TYPES_BALANCE_OPS]
        return {
            "deals": deals,
            "count": len(deals),
            "realized_pnl": snap.state.realized_today,
            "broker_charges": round(sum(auto_rules.deal_net(d) for d in charges), 2),
            "balance_ops": snap.state.balance_ops_today,
            "note": "el día va de 00:00 a 24:00 UTC; realized_pnl incluye operaciones y cargos del broker; los tiempos de los deals están en el reloj del servidor",
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
        entry = tick["ask"] if side == SIDE_BUY else tick["bid"]
        close_ref = tick["bid"] if side == SIDE_BUY else tick["ask"]
        problems = stops_ok(side, close_ref, float(sl), None)
        if problems:
            return {"error": "; ".join(problems), "price": entry}
        try:
            vol, risk_amount, lpl = size_for_risk(self.cfg, spec, acct.equity, entry, float(sl), risk_pct)
        except RiskError as exc:
            return {"error": str(exc), "price": entry}
        return {
            "symbol": spec.name,
            "side": side,
            "price": entry,
            "sl": float(sl),
            "sl_points": round(abs(entry - float(sl)) / spec.point, 1) if spec.point else None,
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
    def _send_market(self, req: dict, summary: dict, ev: Evaluation, events: tuple[str, str, str], **extra: Any) -> dict:
        """order_check del broker y envío. `events` = (rechazo del check, enviado, fallido) para el journal."""
        check = self.b.order_check(req)
        if int(check.get("retcode", -1)) != 0:
            self.journal.record(events[0], summary=summary, check=check, **extra)
            return {"sent": False, "status": "rejected", "error": f"order_check rechazó la orden: {check.get('retcode_name')} {check.get('comment', '')}", "broker_check": check, "summary": summary}
        result = self.b.order_send(req)
        code = int(result.get("retcode", -1))
        status = _status(code)
        ok = status != "failed"
        self.journal.record(events[1] if ok else events[2], status=status, summary=summary, request=req, result=result, warnings=ev.warnings, **extra)
        return {
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

    def order_send(self, symbol: str, side: str, sl: float, tp: float | None = None, volume: float | None = None, comment: str = "", confirm: bool = False, risk_pct: float | None = None) -> dict:
        snap, spec, tick, intent, ev, req = self._prepare(symbol, side, sl, tp, volume, comment, risk_pct)
        summary = self._summary(snap.acct, spec, intent.side, ev)
        if not ev.ok or req is None:
            self.journal.record("order_rejected_by_risk", summary=summary, reasons=ev.reasons)
            return {"sent": False, "status": "rejected", "error": "rechazada por el motor de riesgo", "evaluation": ev.as_dict(), "summary": summary}
        if not confirm:
            self.journal.record("order_pending_confirmation", summary=summary)
            return {"sent": False, "status": "needs_confirmation", "error": CONFIRM_MSG, "evaluation": ev.as_dict(), "summary": summary}
        return self._send_market(req, summary, ev, ("order_rejected_by_broker_check", "order_sent", "order_failed"))

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
        result = self.b.order_send(req)
        code = int(result.get("retcode", -1))
        status = _status(code)
        closed = status == "filled"
        event = {"filled": "position_closed", "partial": "close_partial", "placed": "close_placed"}.get(status, "close_failed")
        self.journal.record(tag + event, position=pos, request=req, result=result, **extra)
        return {
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

    def _send_sltp(self, pos: dict, spec, new_sl: float, new_tp: float, magic: int, events: tuple[str, str], **extra: Any) -> dict:
        m = self._mt5()
        req = {"action": m.TRADE_ACTION_SLTP, "position": int(pos["ticket"]), "symbol": spec.name, "sl": new_sl, "tp": new_tp, "magic": int(magic)}
        result = self.b.order_send(req)
        code = int(result.get("retcode", -1))
        ok = code == RETCODE_DONE
        self.journal.record(events[0] if ok else events[1], position=pos, request=req, result=result, **extra)
        return {"modified": ok, "ticket": pos["ticket"], "retcode": code, "retcode_name": retcode_name(code), "sl": new_sl, "tp": new_tp, "comment": result.get("comment")}

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
        return self._send_sltp(pos, spec, new_sl, new_tp, self.cfg.magic, ("position_modified", "modify_failed"))

    def halt_trading(self, reason: str = "", close_all: bool = False, confirm: bool = False) -> dict:
        self.halt_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = self._now().isoformat(timespec="seconds")
        with self.halt_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {reason or 'halt solicitado'}\n")
        self._halt_latched = True
        closed: list[dict] = []
        errors: list[str] = []
        if close_all and not confirm:
            self.journal.record("halt", reason=reason, close_all=False, note="close_all pedido sin confirm=true; solo se bloqueó")
            return {
                "halted": True,
                "halt_file": str(self.halt_path),
                "closed": [],
                "errors": ["close_all NO ejecutado: cerrar todas las posiciones a mercado exige confirm=true tras confirmación explícita del usuario"],
                "resume": f"para reanudar, un humano debe borrar el archivo {self.halt_path} y reiniciar el servidor MCP",
            }
        if close_all:
            for pos in self.b.positions():
                try:
                    res = self._close(pos)
                    closed.append(res)
                    if not res["closed"]:
                        errors.append(f"{pos['ticket']}: {res['retcode_name']} ({res['status']})")
                except BrokerError as exc:
                    errors.append(f"{pos['ticket']}: {exc}")
        self.journal.record("halt", reason=reason, close_all=close_all, closed=closed, errors=errors)
        return {
            "halted": True,
            "halt_file": str(self.halt_path),
            "closed": closed,
            "errors": errors,
            "resume": f"para reanudar, un humano debe borrar el archivo {self.halt_path} y reiniciar el servidor MCP",
        }

    # ---- modo automático (solo si el usuario lo activó en la configuración) ----------------------
    def _auto_status(self, snap: Snapshot) -> dict:
        a = self.cfg.auto
        st = snap.state
        mode = auto_rules.mode_blockers(self.cfg, snap.acct, st.halted)
        risk = [r for r in trading_blockers(self.cfg, snap.acct, st) if "kill switch" not in r and "allow_live=false" not in r]
        server_now = self.clock.server_now(snap.utc_ts)
        entry = auto_rules.global_entry_blockers(self.cfg, snap.auto, server_now)
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
            "cooldown_remaining_minutes": round(auto_rules.cooldown_remaining(self.cfg, snap.auto, server_now), 1),
            "open_auto_positions": [
                {k: p[k] for k in ("ticket", "symbol", "side", "volume", "price_open", "price_current", "sl", "tp", "profit")}
                for p in snap.auto.open_positions
            ],
            "pnl_today": today,
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
        reasons += auto_rules.global_entry_blockers(self.cfg, snap.auto, self.clock.server_now(snap.utc_ts))
        reasons += auto_rules.symbol_entry_blockers(self.cfg, spec.name, [p["symbol"] for p in snap.positions], self.clock.age(tick["time"], snap.utc_ts))
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
        out = self._send_market(req, summary, ev, ("auto_rejected_by_broker_check", "auto_order_sent", "auto_order_failed"), reason=reason)
        if out.get("sent"):
            # el ticket de la orden de apertura es el identificador de la posición durante toda su vida
            self._register_auto_position(out.get("order"))
        out["next"] = "termina el ciclo; no abras otra operación ni completes ejecuciones parciales"
        return out

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
                    return self._send_sltp(pos, spec, new_sl, float(pos["tp"]), self.cfg.auto_magic, ("auto_stop_tightened", "auto_modify_failed"), reason=reason.strip())
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
