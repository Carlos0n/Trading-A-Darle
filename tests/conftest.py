import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fake_mt5 import FakeMT5  # noqa: E402

from mt5_mcp.broker import MT5Broker  # noqa: E402
from mt5_mcp.config import AutoConfig, RiskConfig  # noqa: E402
from mt5_mcp.engine import Engine  # noqa: E402
from mt5_mcp.journal import Journal  # noqa: E402

T0 = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc).timestamp()  # miércoles a mediodía UTC
WHY = "tendencia alcista en H1 y retroceso a la EMA20 en M15"


class World:
    """Engine + doble de MT5 con un reloj controlado por la prueba.

    El mercado está abierto (EURUSD y GBPUSD cotizan) salvo que la prueba lo pare con `fake.freeze_ticks()`.
    Las esperas del Engine (a que un tick se mueva) no duermen: avanzan este reloj y quedan en `slept`.
    """

    def __init__(self, tmp_path: Path, monkeypatch, *, offset: int = 0, auto: AutoConfig | None = None, clock_wait: float | None = None, **cfg):
        self.t = T0
        self.slept: list[float] = []
        self.monkeypatch = monkeypatch
        self.clock_wait = clock_wait
        self.fake = FakeMT5(server_offset=offset)
        self.fake.clock = lambda: self.t
        self.fake.add_symbol("GBPUSD")
        self.fake.set_tick("GBPUSD", 1.30000, 1.30010)
        base = dict(symbols=("EURUSD", "GBPUSD"), max_open_positions=3, max_volume_per_order=1.0, max_total_volume=3.0)
        base.update(cfg)
        self.cfg = RiskConfig(auto=auto or AutoConfig(enabled=True), **base)
        self.root = tmp_path
        self.engine = self.new_engine()

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds

    def new_engine(self, monkeypatch=None, journal: Journal | None = None) -> Engine:
        """Un Engine nuevo sobre el mismo terminal: como cada proceso que arranca (ciclo del bucle, servidor MCP, panel)."""
        extra = {} if self.clock_wait is None else {"clock_wait": self.clock_wait}
        eng = Engine(MT5Broker(module=self.fake), cfg=self.cfg, root=self.root, journal=journal or Journal(self.root / "journal" / "trades.jsonl"),
                     sleep=self.sleep, **extra)
        (monkeypatch or self.monkeypatch).setattr(eng, "_now", lambda: datetime.fromtimestamp(self.t, tz=timezone.utc))
        return eng

    def advance(self, minutes: float) -> None:
        self.t += minutes * 60

    def buy(self, symbol: str = "EURUSD", **kw):
        px = {"EURUSD": (1.0981, 1.1041), "GBPUSD": (1.2981, 1.3041)}[symbol]
        return self.engine.auto_order(symbol, "buy", kw.pop("sl", px[0]), kw.pop("tp", px[1]), kw.pop("reason", WHY))

    def events(self) -> list[str]:
        return [e["event"] for e in self.engine.journal_tail(50)]


class FailingJournal(Journal):
    """Journal que deja de poder escribirse (disco lleno) a partir de la escritura número `ok_writes` + 1."""

    def __init__(self, path: Path, ok_writes: int = 0):
        super().__init__(path)
        self.ok_writes = ok_writes
        self.attempts: list[str] = []

    def record(self, event: str, **data):
        self.attempts.append(event)
        if len(self.attempts) > self.ok_writes:
            raise OSError(28, "No space left on device")
        return super().record(event, **data)


@pytest.fixture
def cfg() -> RiskConfig:
    return RiskConfig(symbols=("EURUSD", "GBPUSD"), max_open_positions=2, max_volume_per_order=0.5, max_total_volume=1.0)


@pytest.fixture
def fake() -> FakeMT5:
    return FakeMT5()


@pytest.fixture
def engine(fake: FakeMT5, cfg: RiskConfig, tmp_path: Path, monkeypatch) -> Engine:
    # sleep=fake.sleep: la espera a que un tick se mueva (hora del servidor) adelanta el reloj del doble, sin esperar
    # de verdad; con _now=fake.utcnow el engine y el doble comparten ese reloj.
    eng = Engine(MT5Broker(module=fake), cfg=cfg, root=tmp_path, journal=Journal(tmp_path / "journal" / "trades.jsonl"), sleep=fake.sleep)
    monkeypatch.setattr(eng, "_now", fake.utcnow)
    return eng


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    return World(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def no_real_clock_wait(monkeypatch):
    """Ninguna prueba espera de verdad a que un tick se mueva: un Engine creado sin `sleep=` falla en vez de dormir."""
    import time

    def boom(seconds):
        raise AssertionError("Engine creado sin sleep=...: esta prueba dormiría de verdad esperando la hora del servidor")

    defaults = tuple(boom if d is time.sleep else d for d in Engine.__init__.__defaults__)
    assert defaults != Engine.__init__.__defaults__, "Engine ya no usa time.sleep por defecto: revisa esta guarda"
    monkeypatch.setattr(Engine.__init__, "__defaults__", defaults)
