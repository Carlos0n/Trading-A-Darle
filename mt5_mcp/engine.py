"""Lógica de las herramientas MCP (independiente del transporte, para poder probarla)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .broker import BrokerError, MT5Broker, retcode_name
from .config import PROJECT_ROOT, RiskConfig, load_config
from .journal import Journal
from .risk import (
    SIDE_BUY,
    SIDE_SELL,
    Evaluation,
    OrderIntent,
    RiskError,
    RiskState,
    daily_loss_limit,
    evaluate_order,
    normalize_side,
    pick_filling,
    pnl_today,
    size_for_risk,
    stops_ok,
)

RETCODE_DONE = 10009
RETCODE_PLACED = 10008
RETCODE_DONE_PARTIAL = 10010

CONFIRM_MSG = (
    "Orden NO enviada: falta confirm=true. Muestra al usuario el resumen (símbolo, lado, volumen, SL, TP, "
    "riesgo en divisa y %) y llama de nuevo con confirm=true SOLO si el usuario confirma explícitamente."
)


class Engine:
    def __init__(
        self,
        broker: MT5Broker,
        cfg: RiskConfig | None = None,
        root: Path | None = None,
        journal: Journal | None = None,
    ):
        self.b = broker
        self.cfg = cfg or load_config()
        self.root = Path(root or PROJECT_ROOT)
        self.journal = journal or Journal(self.root / self.cfg.journal_file)

    # ---- helpers -------------------------------------------------------------------------------
    @property
    def halt_path(self) -> Path:
        return self.root / self.cfg.halt_file

    def halted(self) -> bool:
        return self.halt_path.exists()

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _state(self) -> RiskState:
        positions = self.b.positions()
        deals = self.b.deals_today(self._now())
        realized = sum(d["profit"] + d["commission"] + d["swap"] for d in deals)
        floating = sum(p["profit"] + p["swap"] for p in positions)
        term = self.b.terminal()
        return RiskState(
            open_positions=len(positions),
            total_volume=round(sum(p["volume"] for p in positions), 4),
            realized_today=round(realized, 2),
            floating_pnl=round(floating, 2),
            halted=self.halted(),
            trade_allowed=term["trade_allowed"],
            now_utc=self._now(),
        )

    def _mt5(self) -> Any:
        return self.b.mt5

    def _market_request(self, spec, side: str, volume: float, price: float, sl: float, tp: float | None, comment: str) -> dict:
        m = self._mt5()
        req = {
            "action": m.TRADE_ACTION_DEAL,
            "symbol": spec.name,
            "volume": float(volume),
            "type": m.ORDER_TYPE_BUY if side == SIDE_BUY else m.ORDER_TYPE_SELL,
            "price": float(price),
            "sl": round(float(sl), spec.digits),
            "tp": round(float(tp), spec.digits) if tp else 0.0,
            "deviation": int(self.cfg.max_slippage_points),
            "magic": int(self.cfg.magic),
            "comment": (comment or "claude")[:31],
            "type_time": m.ORDER_TIME_GTC,
            "type_filling": pick_filling(spec),
        }
        return req

    def _prepare(self, symbol: str, side: str, sl: float, tp: float | None, volume: float | None, comment: str, risk_pct: float | None):
        side = normalize_side(side)
        acct = self.b.account()
        spec = self.b.symbol(symbol)
        tick = self.b.tick(spec.name)
        price = tick["ask"] if side == SIDE_BUY else tick["bid"]
        state = self._state()
        intent = OrderIntent(symbol=spec.name, side=side, sl=float(sl), tp=float(tp) if tp else None, volume=volume, comment=comment, risk_pct=risk_pct)
        ev = evaluate_order(self.cfg, acct, spec, state, intent, price)
        req = self._market_request(spec, side, ev.volume, price, intent.sl, intent.tp, comment) if ev.volume > 0 else None
        return acct, spec, tick, price, state, intent, ev, req

    @staticmethod
    def _summary(acct, spec, side, price, intent: OrderIntent, ev: Evaluation) -> dict:
        return {
            "account": acct.login,
            "mode": acct.mode_name,
            "symbol": spec.name,
            "side": side,
            "price": price,
            "volume": ev.volume,
            "sl": intent.sl,
            "tp": intent.tp,
            "sl_points": ev.sl_points,
            "risk_amount": round(ev.risk_amount, 2),
            "risk_pct": round(ev.risk_pct, 3),
            "currency": acct.currency,
            "spread_points": spec.spread,
        }

    # ---- lecturas ------------------------------------------------------------------------------
    def account_info(self) -> dict:
        acct = self.b.account()
        term = self.b.terminal()
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
            "halted": self.halted(),
            "live_trading_allowed_by_config": self.cfg.allow_live,
            "warning": None if acct.is_demo else "CUENTA REAL: cada orden compromete dinero real",
        }

    def risk_status(self) -> dict:
        acct = self.b.account()
        st = self._state()
        limit = daily_loss_limit(self.cfg, acct, st)
        today = pnl_today(st)
        can_open = not st.halted and st.trade_allowed and today > limit and st.open_positions < self.cfg.max_open_positions and (acct.is_demo or self.cfg.allow_live)
        return {
            "mode": acct.mode_name,
            "halted": st.halted,
            "algo_trading_enabled": st.trade_allowed,
            "can_open_new_positions": can_open,
            "pnl_today": today,
            "realized_today": st.realized_today,
            "floating_pnl": st.floating_pnl,
            "daily_loss_limit": round(limit, 2),
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
        }

    def symbol_info(self, symbol: str) -> dict:
        spec = self.b.symbol(symbol)
        d = spec.__dict__.copy()
        d["allowed_by_config"] = (not self.cfg.symbols) or spec.name in self.cfg.symbols
        d["filling_chosen"] = pick_filling(spec)
        return d

    def quote(self, symbol: str) -> dict:
        spec = self.b.symbol(symbol)
        t = self.b.tick(spec.name)
        spread_points = round((t["ask"] - t["bid"]) / spec.point, 1) if spec.point > 0 else None
        return {"symbol": spec.name, "bid": t["bid"], "ask": t["ask"], "spread_points": spread_points, "time": t["time"], "digits": spec.digits}

    def positions(self, symbol: str | None = None) -> list[dict]:
        return self.b.positions(symbol)

    def pending_orders(self) -> list[dict]:
        return self.b.pending_orders()

    def history_today(self) -> dict:
        deals = self.b.deals_today(self._now())
        realized = round(sum(d["profit"] + d["commission"] + d["swap"] for d in deals), 2)
        return {"deals": deals, "count": len(deals), "realized_pnl": realized}

    def journal_tail(self, n: int = 20) -> list[dict]:
        return self.journal.tail(n)

    # ---- previsualización --------------------------------------------------------------------
    def position_size(self, symbol: str, side: str, sl: float, risk_pct: float | None = None) -> dict:
        side = normalize_side(side)
        acct = self.b.account()
        spec = self.b.symbol(symbol)
        tick = self.b.tick(spec.name)
        price = tick["ask"] if side == SIDE_BUY else tick["bid"]
        problems = stops_ok(side, price, float(sl), None)
        if problems:
            return {"error": "; ".join(problems), "price": price}
        try:
            vol, risk_amount, lpl = size_for_risk(self.cfg, spec, acct.equity, price, float(sl), risk_pct)
        except RiskError as exc:
            return {"error": str(exc), "price": price}
        return {
            "symbol": spec.name,
            "side": side,
            "price": price,
            "sl": float(sl),
            "sl_points": round(abs(price - float(sl)) / spec.point, 1) if spec.point else None,
            "volume": vol,
            "risk_budget": round(risk_amount, 2),
            "actual_risk": round(vol * lpl, 2),
            "loss_per_lot": round(lpl, 2),
            "currency": acct.currency,
            "equity": acct.equity,
            "note": "volumen ya acotado por max_risk_pct_per_trade, max_volume_per_order y volume_max del broker",
        }

    def order_check(self, symbol: str, side: str, sl: float, tp: float | None = None, volume: float | None = None, comment: str = "", risk_pct: float | None = None) -> dict:
        acct, spec, tick, price, state, intent, ev, req = self._prepare(symbol, side, sl, tp, volume, comment, risk_pct)
        out = {"would_send": ev.ok, "evaluation": ev.as_dict(), "summary": self._summary(acct, spec, intent.side, price, intent, ev)}
        if ev.ok and req is not None:
            out["broker_check"] = self.b.order_check(req)
            out["would_send"] = int(out["broker_check"].get("retcode", -1)) == 0
            out["request"] = req
        self.journal.record("order_check", summary=out["summary"], ok=out["would_send"], reasons=ev.reasons)
        return out

    # ---- escrituras ----------------------------------------------------------------------------
    def order_send(self, symbol: str, side: str, sl: float, tp: float | None = None, volume: float | None = None, comment: str = "", confirm: bool = False, risk_pct: float | None = None) -> dict:
        acct, spec, tick, price, state, intent, ev, req = self._prepare(symbol, side, sl, tp, volume, comment, risk_pct)
        summary = self._summary(acct, spec, intent.side, price, intent, ev)
        if not ev.ok or req is None:
            self.journal.record("order_rejected_by_risk", summary=summary, reasons=ev.reasons)
            return {"sent": False, "error": "rechazada por el motor de riesgo", "evaluation": ev.as_dict(), "summary": summary}
        if not confirm:
            self.journal.record("order_pending_confirmation", summary=summary)
            return {"sent": False, "error": CONFIRM_MSG, "evaluation": ev.as_dict(), "summary": summary}
        check = self.b.order_check(req)
        if int(check.get("retcode", -1)) != 0:
            self.journal.record("order_rejected_by_broker_check", summary=summary, check=check)
            return {"sent": False, "error": f"order_check rechazó la orden: {check.get('retcode_name')} {check.get('comment', '')}", "broker_check": check, "summary": summary}
        result = self.b.order_send(req)
        code = int(result.get("retcode", -1))
        ok = code in (RETCODE_DONE, RETCODE_PLACED, RETCODE_DONE_PARTIAL)
        self.journal.record("order_sent" if ok else "order_failed", summary=summary, request=req, result=result, warnings=ev.warnings)
        return {
            "sent": ok,
            "retcode": code,
            "retcode_name": retcode_name(code),
            "order": result.get("order"),
            "deal": result.get("deal"),
            "fill_price": result.get("price"),
            "fill_volume": result.get("volume"),
            "comment": result.get("comment"),
            "summary": summary,
            "warnings": ev.warnings,
            "next": "no reintentes automáticamente; informa al usuario" if not ok else "verifica con positions()",
        }

    def close_position(self, ticket: int, confirm: bool = False) -> dict:
        ticket = int(ticket)
        matches = [p for p in self.b.positions() if p["ticket"] == ticket]
        if not matches:
            return {"closed": False, "error": f"no existe una posición abierta con ticket {ticket}"}
        pos = matches[0]
        if not confirm:
            self.journal.record("close_pending_confirmation", position=pos)
            return {"closed": False, "error": "Cierre NO enviado: falta confirm=true tras confirmación explícita del usuario", "position": pos}
        result = self._close(pos)
        return result

    def _close(self, pos: dict) -> dict:
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
            "magic": int(self.cfg.magic),
            "comment": "claude close",
            "type_time": m.ORDER_TIME_GTC,
            "type_filling": pick_filling(spec),
        }
        result = self.b.order_send(req)
        code = int(result.get("retcode", -1))
        ok = code in (RETCODE_DONE, RETCODE_PLACED, RETCODE_DONE_PARTIAL)
        self.journal.record("position_closed" if ok else "close_failed", position=pos, request=req, result=result)
        return {"closed": ok, "ticket": pos["ticket"], "retcode": code, "retcode_name": retcode_name(code), "fill_price": result.get("price"), "comment": result.get("comment")}

    def modify_position(self, ticket: int, sl: float | None = None, tp: float | None = None, confirm: bool = False) -> dict:
        ticket = int(ticket)
        matches = [p for p in self.b.positions() if p["ticket"] == ticket]
        if not matches:
            return {"modified": False, "error": f"no existe una posición abierta con ticket {ticket}"}
        pos = matches[0]
        new_sl = float(sl) if sl is not None else float(pos["sl"])
        new_tp = float(tp) if tp is not None else float(pos["tp"])
        if new_sl <= 0:
            return {"modified": False, "error": "no se permite quitar el stop loss (sl debe ser > 0)", "position": pos}
        price = pos["price_current"] or pos["price_open"]
        problems = stops_ok(pos["side"], price, new_sl, new_tp if new_tp > 0 else None)
        if problems:
            return {"modified": False, "error": "; ".join(problems), "position": pos}
        if not confirm:
            return {"modified": False, "error": "Modificación NO enviada: falta confirm=true tras confirmación explícita del usuario", "position": pos, "proposed": {"sl": new_sl, "tp": new_tp}}
        m = self._mt5()
        spec = self.b.symbol(pos["symbol"])
        req = {"action": m.TRADE_ACTION_SLTP, "position": ticket, "symbol": spec.name, "sl": round(new_sl, spec.digits), "tp": round(new_tp, spec.digits) if new_tp > 0 else 0.0, "magic": int(self.cfg.magic)}
        result = self.b.order_send(req)
        code = int(result.get("retcode", -1))
        ok = code in (RETCODE_DONE, RETCODE_PLACED)
        self.journal.record("position_modified" if ok else "modify_failed", position=pos, request=req, result=result)
        return {"modified": ok, "ticket": ticket, "retcode": code, "retcode_name": retcode_name(code), "sl": new_sl, "tp": new_tp, "comment": result.get("comment")}

    def halt_trading(self, reason: str = "", close_all: bool = False) -> dict:
        self.halt_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = self._now().isoformat(timespec="seconds")
        with self.halt_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {reason or 'halt solicitado'}\n")
        closed: list[dict] = []
        errors: list[str] = []
        if close_all:
            for pos in self.b.positions():
                try:
                    closed.append(self._close(pos))
                except BrokerError as exc:
                    errors.append(f"{pos['ticket']}: {exc}")
        self.journal.record("halt", reason=reason, close_all=close_all, closed=closed, errors=errors)
        return {
            "halted": True,
            "halt_file": str(self.halt_path),
            "closed": closed,
            "errors": errors,
            "resume": f"para reanudar, un humano debe borrar el archivo {self.halt_path}",
        }
