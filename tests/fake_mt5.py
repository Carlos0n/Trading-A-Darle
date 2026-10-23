"""Módulo falso que imita la API del paquete MetaTrader5 para probar en cualquier SO."""

from __future__ import annotations

from collections import namedtuple
from datetime import datetime

AccountInfo = namedtuple("AccountInfo", "login trade_mode leverage balance equity margin margin_free profit currency server name company")
TerminalInfo = namedtuple("TerminalInfo", "connected trade_allowed company name path build")
SymbolInfo = namedtuple(
    "SymbolInfo",
    "visible digits point spread trade_contract_size trade_tick_value trade_tick_value_loss trade_tick_size volume_min volume_max volume_step filling_mode trade_mode trade_stops_level currency_profit bid ask",
)
Tick = namedtuple("Tick", "time bid ask last volume")
Position = namedtuple("Position", "ticket time type magic volume price_open sl tp price_current swap profit symbol comment")
Deal = namedtuple("Deal", "ticket order time type entry magic position_id volume price commission swap profit symbol")
OrderCheckResult = namedtuple("OrderCheckResult", "retcode balance equity profit margin margin_free margin_level comment request")
OrderSendResult = namedtuple("OrderSendResult", "retcode deal order volume price bid ask comment request_id request")


class FakeMT5:
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_SLTP = 6
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    TRADE_RETCODE_DONE = 10009
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_REAL = 2

    def __init__(self, trade_mode: int = 0, balance: float = 10_000.0, equity: float | None = None, trade_allowed: bool = True):
        self.initialized = False
        self.init_kwargs: dict | None = None
        self.fail_init = False
        self._account = AccountInfo(
            login=12345678, trade_mode=trade_mode, leverage=100, balance=balance, equity=balance if equity is None else equity,
            margin=0.0, margin_free=balance, profit=0.0, currency="USD", server="Demo-Server", name="Test", company="Broker",
        )
        self._terminal = TerminalInfo(connected=True, trade_allowed=trade_allowed, company="Broker", name="MetaTrader 5", path="C:\\MT5", build=5000)
        self.symbols: dict[str, SymbolInfo] = {}
        self.ticks: dict[str, Tick] = {}
        self._positions: list[Position] = []
        self.deals: list[Deal] = []
        self.sent: list[dict] = []
        self.checked: list[dict] = []
        self.send_retcode = 10009
        self.check_retcode = 0
        self.next_ticket = 1000
        self.add_symbol("EURUSD")
        self.set_tick("EURUSD", 1.10000, 1.10010)

    # ---- fixtures ---------------------------------------------------------------------------
    def add_symbol(self, name: str, *, digits: int = 5, point: float = 0.00001, tick_value: float = 1.0, tick_value_loss: float | None = None, tick_size: float = 0.00001,
                   volume_min: float = 0.01, volume_max: float = 100.0, volume_step: float = 0.01, filling_mode: int = 1,
                   trade_mode: int = 4, stops_level: int = 0, spread: int = 10, visible: bool = True) -> None:
        self.symbols[name] = SymbolInfo(
            visible=visible, digits=digits, point=point, spread=spread, trade_contract_size=100_000.0, trade_tick_value=tick_value,
            trade_tick_value_loss=tick_value if tick_value_loss is None else tick_value_loss, trade_tick_size=tick_size, volume_min=volume_min, volume_max=volume_max, volume_step=volume_step, filling_mode=filling_mode,
            trade_mode=trade_mode, trade_stops_level=stops_level, currency_profit="USD", bid=0.0, ask=0.0,
        )

    def set_tick(self, name: str, bid: float, ask: float) -> None:
        self.ticks[name] = Tick(time=1_700_000_000, bid=bid, ask=ask, last=bid, volume=0)

    def add_position(self, symbol: str, side: str, volume: float, price_open: float, sl: float = 0.0, tp: float = 0.0, profit: float = 0.0, magic: int = 0) -> int:
        ticket = self.next_ticket
        self.next_ticket += 1
        tick = self.ticks.get(symbol)
        current = (tick.bid if side == "buy" else tick.ask) if tick else price_open
        self._positions.append(Position(ticket=ticket, time=1_700_000_000, type=0 if side == "buy" else 1, magic=magic, volume=volume,
                                        price_open=price_open, sl=sl, tp=tp, price_current=current, swap=0.0, profit=profit, symbol=symbol, comment=""))
        return ticket

    def add_deal(self, profit: float, commission: float = 0.0, swap: float = 0.0, entry: int = 1, deal_type: int = 1, symbol: str = "EURUSD") -> None:
        self.deals.append(Deal(ticket=len(self.deals) + 1, order=0, time=1_700_000_000, type=deal_type, entry=entry, magic=0, position_id=0,
                               volume=0.1, price=1.1, commission=commission, swap=swap, profit=profit, symbol=symbol))

    def set_account(self, **changes) -> None:
        self._account = self._account._replace(**changes)

    def set_terminal(self, **changes) -> None:
        self._terminal = self._terminal._replace(**changes)

    # ---- API MetaTrader5 --------------------------------------------------------------------
    def initialize(self, **kwargs):
        self.init_kwargs = kwargs
        if self.fail_init:
            return False
        self.initialized = True
        return True

    def shutdown(self):
        self.initialized = False

    def last_error(self):
        return (-10005, "IPC timeout") if self.fail_init else (1, "Success")

    def terminal_info(self):
        return self._terminal if self.initialized else None

    def account_info(self):
        return self._account if self.initialized else None

    def symbol_info(self, name):
        return self.symbols.get(name)

    def symbol_select(self, name, enable=True):
        if name not in self.symbols:
            return False
        self.symbols[name] = self.symbols[name]._replace(visible=enable)
        return True

    def symbol_info_tick(self, name):
        return self.ticks.get(name)

    def positions_get(self, symbol=None, ticket=None):
        out = list(self._positions)
        if symbol:
            out = [p for p in out if p.symbol == symbol]
        if ticket:
            out = [p for p in out if p.ticket == ticket]
        return tuple(out)

    def orders_get(self):
        return ()

    def history_deals_get(self, date_from, date_to):
        assert isinstance(date_from, datetime) and isinstance(date_to, datetime)
        return tuple(self.deals)

    def order_check(self, request):
        self.checked.append(dict(request))
        return OrderCheckResult(retcode=self.check_retcode, balance=self._account.balance, equity=self._account.equity, profit=0.0,
                                margin=10.0, margin_free=self._account.margin_free, margin_level=0.0,
                                comment="Done" if self.check_retcode == 0 else "Rejected", request=request)

    def order_send(self, request):
        self.sent.append(dict(request))
        code = self.send_retcode
        if code == 10009:
            if request.get("action") == self.TRADE_ACTION_DEAL and "position" in request:
                self._positions = [p for p in self._positions if p.ticket != request["position"]]
            elif request.get("action") == self.TRADE_ACTION_DEAL:
                side = "buy" if request["type"] == self.ORDER_TYPE_BUY else "sell"
                self.add_position(request["symbol"], side, request["volume"], request["price"], request.get("sl", 0.0), request.get("tp", 0.0), magic=request.get("magic", 0))
            elif request.get("action") == self.TRADE_ACTION_SLTP:
                self._positions = [p._replace(sl=request["sl"], tp=request["tp"]) if p.ticket == request["position"] else p for p in self._positions]
        return OrderSendResult(retcode=code, deal=555 if code == 10009 else 0, order=777 if code == 10009 else 0, volume=request.get("volume", 0.0),
                               price=request.get("price", 0.0), bid=0.0, ask=0.0, comment="Request executed" if code == 10009 else "Rejected",
                               request_id=1, request=request)
