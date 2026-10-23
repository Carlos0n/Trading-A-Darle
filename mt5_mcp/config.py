"""Carga de la configuración de riesgo (risk.toml)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path


def _project_root() -> Path:
    """Raíz del repo.

    Orden: CLAUDE_PROJECT_DIR (lo define Claude Code al lanzar el servidor y no es manipulable desde .mcp.json),
    después MT5_MCP_ROOT solo para los scripts de PowerShell (cuando Claude Code no ha definido nada),
    y por último la carpeta que contiene este paquete (instalación editable).
    """
    claude_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if claude_dir and Path(claude_dir).is_dir():
        return Path(claude_dir).resolve()
    manual = os.environ.get("MT5_MCP_ROOT")
    if not claude_dir and manual and Path(manual).is_dir():
        return Path(manual).resolve()
    return Path(__file__).resolve().parents[1]


PROJECT_ROOT = _project_root()
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "risk.toml"


@dataclass(frozen=True)
class RiskConfig:
    """Límites que el servidor aplica en código antes de cada orden."""

    allow_live: bool = False
    max_risk_pct_per_trade: float = 1.0
    max_daily_loss_pct: float = 3.0
    max_open_positions: int = 3
    max_volume_per_order: float = 0.10
    max_total_volume: float = 0.30
    symbols: tuple[str, ...] = ()
    require_tp: bool = False
    min_sl_points: int = 0
    max_spread_points: int = 0
    max_slippage_points: int = 20
    trading_hours_utc: tuple[int, int] | None = None
    magic: int = 20261023
    halt_file: str = "HALT"
    journal_file: str = "journal/trades.jsonl"

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not 0 < self.max_risk_pct_per_trade <= 5:
            problems.append("max_risk_pct_per_trade debe estar entre 0 y 5")
        if not 0 < self.max_daily_loss_pct <= 20:
            problems.append("max_daily_loss_pct debe estar entre 0 y 20")
        if self.max_open_positions < 1:
            problems.append("max_open_positions debe ser >= 1")
        if self.max_volume_per_order <= 0:
            problems.append("max_volume_per_order debe ser > 0")
        if self.max_total_volume < self.max_volume_per_order:
            problems.append("max_total_volume debe ser >= max_volume_per_order")
        if self.max_slippage_points < 0:
            problems.append("max_slippage_points debe ser >= 0")
        if self.trading_hours_utc is not None:
            start, end = self.trading_hours_utc
            if not (0 <= start <= 24 and 0 <= end <= 24):
                problems.append("trading_hours_utc debe contener horas entre 0 y 24")
        return problems


def _from_dict(data: dict) -> RiskConfig:
    cfg = RiskConfig()
    hours = data.get("trading_hours_utc")
    hours_t: tuple[int, int] | None
    if hours:
        if len(hours) != 2:
            raise ValueError("trading_hours_utc debe ser [inicio, fin] o lista vacía")
        hours_t = (int(hours[0]), int(hours[1]))
    else:
        hours_t = None
    cfg = replace(
        cfg,
        allow_live=bool(data.get("allow_live", cfg.allow_live)),
        max_risk_pct_per_trade=float(data.get("max_risk_pct_per_trade", cfg.max_risk_pct_per_trade)),
        max_daily_loss_pct=float(data.get("max_daily_loss_pct", cfg.max_daily_loss_pct)),
        max_open_positions=int(data.get("max_open_positions", cfg.max_open_positions)),
        max_volume_per_order=float(data.get("max_volume_per_order", cfg.max_volume_per_order)),
        max_total_volume=float(data.get("max_total_volume", cfg.max_total_volume)),
        # Los nombres de símbolo son exactos en MT5 (EURUSD.m, XAUUSD.raw): se guardan tal cual y se comparan sin mayúsculas
        symbols=tuple(str(s).strip() for s in data.get("symbols", []) if str(s).strip()),
        require_tp=bool(data.get("require_tp", cfg.require_tp)),
        min_sl_points=int(data.get("min_sl_points", cfg.min_sl_points)),
        max_spread_points=int(data.get("max_spread_points", cfg.max_spread_points)),
        max_slippage_points=int(data.get("max_slippage_points", cfg.max_slippage_points)),
        trading_hours_utc=hours_t,
        magic=int(data.get("magic", cfg.magic)),
        halt_file=str(data.get("halt_file", cfg.halt_file)),
        journal_file=str(data.get("journal_file", cfg.journal_file)),
    )
    problems = cfg.validate()
    if problems:
        raise ValueError("risk.toml inválido: " + "; ".join(problems))
    return cfg


def load_config(path: str | os.PathLike | None = None) -> RiskConfig:
    """Lee risk.toml de la raíz del proyecto (o la ruta indicada). Sin archivo, usa los valores por defecto."""
    p = Path(path or DEFAULT_CONFIG_PATH)
    if not p.exists():
        return _from_dict({})
    with p.open("rb") as fh:
        return _from_dict(tomllib.load(fh))
