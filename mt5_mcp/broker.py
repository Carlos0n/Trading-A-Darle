"""Envoltorio fino sobre el paquete oficial MetaTrader5 (MetaQuotes).

Todo acceso al terminal pasa por aquí, de modo que las pruebas puedan inyectar un módulo falso.
Regla de oro: cualquier función de consulta que devuelva None es un ERROR del terminal (así lo documenta
MetaQuotes), nunca "no hay datos"; aquí se convierte en BrokerError para que el motor de riesgo no decida
con un estado vacío.
"""

from __future__ import annotations

import importlib
import os
from datetime import datetime, timezone
from typing import Any

from .risk import AccountSnapshot, SymbolSpec

DEAL_TYPE_BUY = 0
DEAL_TYPE_SELL = 1
TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1")
# Deals que mueven el balance sin ser operaciones: depósitos/retiradas, crédito, corrección, bono
DEAL_TYPES_BALANCE_OPS = (2, 3, 5, 6)

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


def _field(row: Any, name: str) -> Any:
    """Lee un campo de una fila de velas: registro de numpy (row["close"]), dict o namedtuple."""
    try:
        return row[name]
    except (TypeError, KeyError, IndexError, ValueError):
        return getattr(row, name)


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
            trade_allowed=bool(d.get("trade_allowed", True)),
            trade_expert=bool(d.get("trade_expert", True)),
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
            "tradeapi_disabled": bool(d.get("tradeapi_disabled", False)),
            "company": d.get("company", ""),
            "name": d.get("name", ""),
            "path": d.get("path", ""),
            "build": d.get("build", 0),
        }

    def resolve_symbol(self, name: str) -> str:
        """Devuelve el nombre exacto del símbolo en este broker (EURUSD.m, XAUUSD.raw...), sin alterar mayúsculas."""
        self.ensure_connected()
        name = (name or "").strip()
        if not name:
            raise BrokerError("símbolo vacío")
        if self.mt5.symbol_info(name) is not None:
            return name
        all_symbols = self.mt5.symbols_get() if hasattr(self.mt5, "symbols_get") else None
        candidates = [s.name for s in (all_symbols or ()) if s.name.casefold() == name.casefold()]
        if len(candidates) == 1:
            return candidates[0]
        similar = [s.name for s in (all_symbols or ()) if name.casefold() in s.name.casefold()][:8]
        hint = f"; parecidos: {', '.join(similar)}" if similar else ""
        raise BrokerError(f"símbolo {name} no existe en este broker (usa el nombre exacto de Observación de Mercado){hint}")

    def symbol(self, name: str) -> SymbolSpec:
        exact = self.resolve_symbol(name)
        info = self.mt5.symbol_info(exact)
        if info is None:
            raise self._error(f"symbol_info({exact}) devolvió None")
        d = _asdict(info)
        if not d.get("visible", True):
            if not self.mt5.symbol_select(exact, True):
                raise self._error(f"no se pudo añadir {exact} a Market Watch")
            info = self.mt5.symbol_info(exact)
            if info is None:
                raise self._error(f"symbol_info({exact}) devolvió None tras symbol_select")
            d = _asdict(info)
        return SymbolSpec(
            name=exact,
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
        t = self.mt5.symbol_info_tick(name)
        if t is None:
            raise self._error(f"symbol_info_tick({name}) devolvió None (¿símbolo fuera de Observación de Mercado?)")
        d = _asdict(t)
        # `time` viene en el reloj del servidor del broker (no en UTC real): ver timeutil.py
        return {"bid": float(d.get("bid", 0.0)), "ask": float(d.get("ask", 0.0)), "time": int(d.get("time", 0))}

    def rates(self, name: str, timeframe: str, count: int) -> list[dict]:
        """Últimas `count` velas, de la más antigua a la más reciente; la última es la vela en formación."""
        self.ensure_connected()
        tf = (timeframe or "").strip().upper()
        if tf not in TIMEFRAMES:
            raise BrokerError(f"timeframe inválido: {timeframe!r}; usa uno de {', '.join(TIMEFRAMES)}")
        const = getattr(self.mt5, f"TIMEFRAME_{tf}", None)
        if const is None:
            raise BrokerError(f"el paquete MetaTrader5 no define TIMEFRAME_{tf}")
        raw = self.mt5.copy_rates_from_pos(name, const, 0, int(count))
        if raw is None:
            raise self._error(f"copy_rates_from_pos({name}, {tf}) devolvió None")
        out = []
        for row in raw:
            out.append(
                {
                    "time": int(_field(row, "time")),
                    "open": float(_field(row, "open")),
                    "high": float(_field(row, "high")),
                    "low": float(_field(row, "low")),
                    "close": float(_field(row, "close")),
                }
            )
        return out

    def positions(self, symbol: str | None = None) -> list[dict]:
        self.ensure_connected()
        raw = self.mt5.positions_get(symbol=self.resolve_symbol(symbol)) if symbol else self.mt5.positions_get()
        if raw is None:
            raise self._error("positions_get() devolvió None")
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
            raise self._error("orders_get() devolvió None")
        return [_asdict(o) for o in raw]

    def deals(self, since: int, until: int) -> list[dict]:
        """Todos los deals entre dos instantes expresados en el reloj del SERVIDOR (segundos desde 1970).

        Incluye operaciones (type 0/1) y movimientos de balance (depósitos, crédito, bonos); `None` es error.
        """
        self.ensure_connected()
        date_from = datetime.fromtimestamp(int(since), tz=timezone.utc)
        date_to = datetime.fromtimestamp(int(until), tz=timezone.utc)
        raw = self.mt5.history_deals_get(date_from, date_to)
        if raw is None:
            raise self._error("history_deals_get() devolvió None")
        out: list[dict] = []
        for deal in raw:
            d = _asdict(deal)
            out.append(
                {
                    "ticket": int(d.get("ticket", 0)),
                    "type": int(d.get("type", -1)),
                    "position_id": int(d.get("position_id", 0)),
                    "symbol": d.get("symbol", ""),
                    "entry": int(d.get("entry", 0)),
                    "reason": int(d.get("reason", 0)),
                    "volume": float(d.get("volume", 0.0)),
                    "price": float(d.get("price", 0.0)),
                    "profit": float(d.get("profit", 0.0)),
                    "commission": float(d.get("commission", 0.0)),
                    "swap": float(d.get("swap", 0.0)),
                    "fee": float(d.get("fee", 0.0)),
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
