"""Envoltorio fino sobre el paquete oficial MetaTrader5 (MetaQuotes).

Todo acceso al terminal pasa por aquí, de modo que las pruebas puedan inyectar un módulo falso.
"""

from __future__ import annotations

import importlib
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from .risk import AccountSnapshot, SymbolSpec

DEAL_TYPE_BUY = 0
DEAL_TYPE_SELL = 1

RETCODE_NAMES = {
    10004: "REQUOTE",
    10006: "REJECT",
    10007: "CANCEL",
    10008: "PLACED",
    10009: "DONE",
    10010: "DONE_PARTIAL",
    10011: "ERROR",
    10012: "TIMEOUT",
    10013: "INVALID",
    10014: "INVALID_VOLUME",
    10015: "INVALID_PRICE",
    10016: "INVALID_STOPS",
    10017: "TRADE_DISABLED",
    10018: "MARKET_CLOSED",
    10019: "NO_MONEY",
    10020: "PRICE_CHANGED",
    10021: "PRICE_OFF",
    10022: "INVALID_EXPIRATION",
    10023: "ORDER_CHANGED",
    10024: "TOO_MANY_REQUESTS",
    10025: "NO_CHANGES",
    10026: "SERVER_DISABLES_AT",
    10027: "CLIENT_DISABLES_AT",
    10028: "LOCKED",
    10029: "FROZEN",
    10030: "INVALID_FILL",
    10031: "CONNECTION",
    10032: "ONLY_REAL",
    10033: "LIMIT_ORDERS",
    10034: "LIMIT_VOLUME",
    10035: "INVALID_ORDER",
    10036: "POSITION_CLOSED",
    10038: "INVALID_CLOSE_VOLUME",
    10039: "CLOSE_ORDER_EXIST",
    10040: "LIMIT_POSITIONS",
    10041: "REJECT_CANCEL",
    10042: "LONG_ONLY",
    10043: "SHORT_ONLY",
    10044: "CLOSE_ONLY",
    10045: "FIFO_CLOSE",
    10046: "HEDGE_PROHIBITED",
}


def retcode_name(code: int) -> str:
    return RETCODE_NAMES.get(int(code), f"RETCODE_{code}")


class BrokerError(RuntimeError):
    """Fallo de comunicación con el terminal MT5."""


def _asdict(obj: Any) -> dict:
    if obj is None:
        return {}
    if hasattr(obj, "_asdict"):
        return dict(obj._asdict())
    if isinstance(obj, dict):
        return dict(obj)
    return {k: getattr(obj, k) for k in dir(obj) if not k.startswith("_") and not callable(getattr(obj, k))}


class MT5Broker:
    def __init__(self, module: Any | None = None):
        if module is None:
            try:
                module = importlib.import_module("MetaTrader5")
            except ImportError as exc:  # pragma: no cover - depende del SO
                raise BrokerError(
                    "No se pudo importar el paquete MetaTrader5. Solo existe para Windows x64: "
                    "ejecuta scripts/setup.ps1 en el PC donde está instalado MetaTrader 5."
                ) from exc
        self.mt5 = module
        self._connected = False

    # ---- conexión -----------------------------------------------------------------------------
    def connect(self) -> None:
        kwargs: dict[str, Any] = {}
        path = os.environ.get("MT5_TERMINAL_PATH")
        if path:
            kwargs["path"] = path
        login = os.environ.get("MT5_LOGIN")
        if login:
            kwargs["login"] = int(login)
            kwargs["password"] = os.environ.get("MT5_PASSWORD", "")
            kwargs["server"] = os.environ.get("MT5_SERVER", "")
        if not self.mt5.initialize(**kwargs):
            code, desc = self.mt5.last_error()
            raise BrokerError(
                f"initialize() falló ({code}: {desc}). Comprueba que MetaTrader 5 está abierto y con sesión iniciada; "
                "si hay varias instalaciones, define MT5_TERMINAL_PATH con la ruta a terminal64.exe"
            )
        self._connected = True

    def ensure_connected(self) -> None:
        if not self._connected:
            self.connect()
            return
        if self.mt5.terminal_info() is None:
            self._connected = False
            self.connect()

    def shutdown(self) -> None:
        if self._connected:
            self.mt5.shutdown()
            self._connected = False

    def _error(self, what: str) -> BrokerError:
        code, desc = self.mt5.last_error()
        return BrokerError(f"{what}: {code} {desc}")

    # ---- lecturas -----------------------------------------------------------------------------
    def account(self) -> AccountSnapshot:
        self.ensure_connected()
        info = self.mt5.account_info()
        if info is None:
            raise self._error("account_info() devolvió None")
        d = _asdict(info)
        return AccountSnapshot(
            login=int(d.get("login", 0)),
            trade_mode=int(d.get("trade_mode", 2)),
            balance=float(d.get("balance", 0.0)),
            equity=float(d.get("equity", 0.0)),
            currency=str(d.get("currency", "")),
            server=str(d.get("server", "")),
            leverage=int(d.get("leverage", 0)),
            margin_free=float(d.get("margin_free", 0.0)),
        )

    def terminal(self) -> dict:
        self.ensure_connected()
        info = self.mt5.terminal_info()
        if info is None:
            raise self._error("terminal_info() devolvió None")
        d = _asdict(info)
        return {
            "connected": bool(d.get("connected", False)),
            "trade_allowed": bool(d.get("trade_allowed", False)),
            "company": d.get("company", ""),
            "name": d.get("name", ""),
            "path": d.get("path", ""),
            "build": d.get("build", 0),
        }

    def symbol(self, name: str) -> SymbolSpec:
        self.ensure_connected()
        name = name.upper()
        info = self.mt5.symbol_info(name)
        if info is None:
            raise BrokerError(f"símbolo {name} no existe en este broker (symbol_info devolvió None)")
        d = _asdict(info)
        if not d.get("visible", True):
            if not self.mt5.symbol_select(name, True):
                raise self._error(f"no se pudo añadir {name} a Market Watch")
            d = _asdict(self.mt5.symbol_info(name))
        return SymbolSpec(
            name=name,
            digits=int(d.get("digits", 5)),
            point=float(d.get("point", 0.0)),
            tick_size=float(d.get("trade_tick_size", 0.0)),
            tick_value=float(d.get("trade_tick_value", 0.0)),
            volume_min=float(d.get("volume_min", 0.01)),
            volume_max=float(d.get("volume_max", 100.0)),
            volume_step=float(d.get("volume_step", 0.01)),
            stops_level=int(d.get("trade_stops_level", 0)),
            spread=int(d.get("spread", 0)),
            filling_mode=int(d.get("filling_mode", 0)),
            trade_mode=int(d.get("trade_mode", 4)),
            contract_size=float(d.get("trade_contract_size", 0.0)),
            currency_profit=str(d.get("currency_profit", "")),
            tick_value_loss=float(d.get("trade_tick_value_loss", 0.0)),
        )

    def tick(self, name: str) -> dict:
        self.ensure_connected()
        t = self.mt5.symbol_info_tick(name.upper())
        if t is None:
            raise self._error(f"symbol_info_tick({name}) devolvió None")
        d = _asdict(t)
        return {"bid": float(d.get("bid", 0.0)), "ask": float(d.get("ask", 0.0)), "time": int(d.get("time", 0))}

    def positions(self, symbol: str | None = None) -> list[dict]:
        self.ensure_connected()
        raw = self.mt5.positions_get(symbol=symbol.upper()) if symbol else self.mt5.positions_get()
        if raw is None:
            return []
        out = []
        for p in raw:
            d = _asdict(p)
            out.append(
                {
                    "ticket": int(d.get("ticket", 0)),
                    "symbol": d.get("symbol", ""),
                    "side": "buy" if int(d.get("type", 0)) == 0 else "sell",
                    "volume": float(d.get("volume", 0.0)),
                    "price_open": float(d.get("price_open", 0.0)),
                    "price_current": float(d.get("price_current", 0.0)),
                    "sl": float(d.get("sl", 0.0)),
                    "tp": float(d.get("tp", 0.0)),
                    "profit": float(d.get("profit", 0.0)),
                    "swap": float(d.get("swap", 0.0)),
                    "magic": int(d.get("magic", 0)),
                    "comment": d.get("comment", ""),
                    "time": int(d.get("time", 0)),
                }
            )
        return out

    def pending_orders(self) -> list[dict]:
        self.ensure_connected()
        raw = self.mt5.orders_get()
        if raw is None:
            return []
        return [_asdict(o) for o in raw]

    def deals_today(self, now_utc: datetime | None = None) -> list[dict]:
        """Deals de hoy (día UTC). Solo operaciones de compra/venta, no depósitos ni ajustes."""
        self.ensure_connected()
        now = now_utc or datetime.now(timezone.utc)
        start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        end = now + timedelta(days=1)
        raw = self.mt5.history_deals_get(start, end)
        if raw is None:
            return []
        out = []
        for deal in raw:
            d = _asdict(deal)
            if int(d.get("type", -1)) not in (DEAL_TYPE_BUY, DEAL_TYPE_SELL):
                continue
            out.append(
                {
                    "ticket": int(d.get("ticket", 0)),
                    "position_id": int(d.get("position_id", 0)),
                    "symbol": d.get("symbol", ""),
                    "entry": int(d.get("entry", 0)),
                    "volume": float(d.get("volume", 0.0)),
                    "price": float(d.get("price", 0.0)),
                    "profit": float(d.get("profit", 0.0)),
                    "commission": float(d.get("commission", 0.0)),
                    "swap": float(d.get("swap", 0.0)),
                    "magic": int(d.get("magic", 0)),
                    "time": int(d.get("time", 0)),
                }
            )
        return out

    # ---- escrituras ---------------------------------------------------------------------------
    def order_check(self, request: dict) -> dict:
        self.ensure_connected()
        res = self.mt5.order_check(request)
        if res is None:
            raise self._error("order_check() devolvió None")
        d = _asdict(res)
        d.pop("request", None)
        d["retcode_name"] = "OK" if int(d.get("retcode", -1)) == 0 else retcode_name(int(d.get("retcode", -1)))
        return d

    def order_send(self, request: dict) -> dict:
        self.ensure_connected()
        res = self.mt5.order_send(request)
        if res is None:
            raise self._error("order_send() devolvió None")
        d = _asdict(res)
        d.pop("request", None)
        d["retcode_name"] = retcode_name(int(d.get("retcode", -1)))
        return d
