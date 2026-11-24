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
class AutoConfig:
    """Modo automático (tabla [auto] del archivo de configuración). Desactivado salvo que el usuario lo active a mano."""

    enabled: bool = False
    allow_live: bool = False  # operar en automático con cuenta REAL exige esto Y allow_live general
    risk_pct: float = 0.5  # % del equity por operación automática (nunca supera max_risk_pct_per_trade)
    max_trades_per_day: int = 4
    max_consecutive_losses: int = 2  # tras N pérdidas seguidas hoy, no abre más hasta mañana (UTC)
    cooldown_minutes: int = 15  # espera mínima entre dos entradas automáticas
    min_rr: float = 1.5  # ratio beneficio/riesgo mínimo (TP obligatorio)
    one_position_per_symbol: bool = True
    max_quote_age_seconds: int = 60  # no decide con cotizaciones más viejas que esto
    magic: int = 0  # 0 = magic general + 1

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not 0 < self.risk_pct <= 5:
            problems.append("auto.risk_pct debe estar entre 0 y 5")
        if not 1 <= self.max_trades_per_day <= 50:
            problems.append("auto.max_trades_per_day debe estar entre 1 y 50")
        if not 1 <= self.max_consecutive_losses <= 20:
            problems.append("auto.max_consecutive_losses debe estar entre 1 y 20")
        if not 0 <= self.cooldown_minutes <= 1440:
            problems.append("auto.cooldown_minutes debe estar entre 0 y 1440")
        if not 0.5 <= self.min_rr <= 10:
            problems.append("auto.min_rr debe estar entre 0.5 y 10")
        if not 5 <= self.max_quote_age_seconds <= 600:
            problems.append("auto.max_quote_age_seconds debe estar entre 5 y 600")
        if self.magic < 0:
            problems.append("auto.magic debe ser >= 0")
        return problems


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
    auto: AutoConfig = AutoConfig()

    @property
    def auto_magic(self) -> int:
        """Identificador de las órdenes del modo automático (distinto del de las órdenes confirmadas a mano)."""
        return self.auto.magic or self.magic + 1

    @property
    def auto_risk_pct(self) -> float:
        return min(self.auto.risk_pct, self.max_risk_pct_per_trade)

    def validate(self) -> list[str]:
        problems: list[str] = list(self.auto.validate())
        if self.auto.magic and self.auto.magic == self.magic:
            problems.append("auto.magic debe ser distinto de magic")
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


def _auto_from_dict(data: dict) -> AutoConfig:
    if not isinstance(data, dict):
        raise ValueError("[auto] debe ser una tabla")
    d = AutoConfig()
    unknown = sorted(set(data) - set(AutoConfig.__dataclass_fields__))
    if unknown:
        raise ValueError(f"[auto]: claves desconocidas {unknown}")
    return AutoConfig(
        enabled=bool(data.get("enabled", d.enabled)),
        allow_live=bool(data.get("allow_live", d.allow_live)),
        risk_pct=float(data.get("risk_pct", d.risk_pct)),
        max_trades_per_day=int(data.get("max_trades_per_day", d.max_trades_per_day)),
        max_consecutive_losses=int(data.get("max_consecutive_losses", d.max_consecutive_losses)),
        cooldown_minutes=int(data.get("cooldown_minutes", d.cooldown_minutes)),
        min_rr=float(data.get("min_rr", d.min_rr)),
        one_position_per_symbol=bool(data.get("one_position_per_symbol", d.one_position_per_symbol)),
        max_quote_age_seconds=int(data.get("max_quote_age_seconds", d.max_quote_age_seconds)),
        magic=int(data.get("magic", d.magic)),
    )


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
        auto=_auto_from_dict(data.get("auto", {})),
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
