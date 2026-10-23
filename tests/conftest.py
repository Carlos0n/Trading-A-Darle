import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fake_mt5 import FakeMT5  # noqa: E402

from mt5_mcp.broker import MT5Broker  # noqa: E402
from mt5_mcp.config import RiskConfig  # noqa: E402
from mt5_mcp.engine import Engine  # noqa: E402
from mt5_mcp.journal import Journal  # noqa: E402


@pytest.fixture
def cfg() -> RiskConfig:
    return RiskConfig(symbols=("EURUSD", "GBPUSD"), max_open_positions=2, max_volume_per_order=0.5, max_total_volume=1.0)


@pytest.fixture
def fake() -> FakeMT5:
    return FakeMT5()


@pytest.fixture
def engine(fake: FakeMT5, cfg: RiskConfig, tmp_path: Path) -> Engine:
    return Engine(MT5Broker(module=fake), cfg=cfg, root=tmp_path, journal=Journal(tmp_path / "journal" / "trades.jsonl"))
