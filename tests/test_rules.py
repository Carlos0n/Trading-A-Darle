"""Reglas puras de configuración, estadísticas del bot y límite diario (casos de la segunda revisión)."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from mt5_mcp import auto as rules
from mt5_mcp.config import AutoConfig, RiskConfig, load_config
from mt5_mcp.risk import AccountSnapshot, RiskState, daily_loss_hit, daily_loss_limit

DAY = int(datetime(2026, 10, 22, tzinfo=timezone.utc).timestamp())


def _toml(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "risk.toml"
    p.write_text(text, encoding="utf-8")
    return p


def _deal(pos, entry, t, magic=77, profit=0.0, ticket=0):
    return {"ticket": ticket or t, "position_id": pos, "entry": entry, "time": t, "magic": magic, "symbol": "EURUSD",
            "profit": profit, "commission": 0.0, "swap": 0.0, "fee": 0.0, "type": 0, "volume": 0.1, "price": 1.1}


# ---- interruptores de risk.toml ---------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    '[auto]\nenabled = "false"\n',
    '[auto]\nenabled = "true"\n',
    '[auto]\nenabled = 1\n',
    '[auto]\nenabled = true\nallow_live = "no"\n',
    '[auto]\none_position_per_symbol = "false"\n',
    'allow_live = "false"\n',
    'allow_live = 0\n',
    'require_tp = "true"\n',
])
def test_quoted_or_numeric_switches_are_rejected(tmp_path, text):
    with pytest.raises(ValueError, match="true o false, sin comillas"):
        load_config(_toml(tmp_path, text))


def test_plain_boolean_switches_load(tmp_path):
    cfg = load_config(_toml(tmp_path, "allow_live = false\nrequire_tp = true\n[auto]\nenabled = true\nallow_live = false\none_position_per_symbol = false\n"))
    assert (cfg.allow_live, cfg.require_tp) == (False, True)
    assert (cfg.auto.enabled, cfg.auto.allow_live, cfg.auto.one_position_per_symbol) == (True, False, False)


@pytest.mark.parametrize("magic", [0, -1, -20261023])
def test_magic_must_be_positive(tmp_path, magic):
    # con magic -1 el bot llevaría magic 0, el de las órdenes hechas a mano en el terminal
    with pytest.raises(ValueError, match="magic debe ser >= 1"):
        load_config(_toml(tmp_path, f"magic = {magic}\n"))
    assert "magic debe ser >= 1" in RiskConfig(magic=magic).validate()
    assert RiskConfig(magic=1).validate() == []
    assert RiskConfig(magic=1).auto_magic == 2


# ---- estadísticas del bot --------------------------------------------------------------------------


def test_cooldown_reference_survives_midnight():
    yesterday_2350 = DAY - 600
    deals = [_deal(10, rules.DEAL_ENTRY_IN, yesterday_2350)]
    stats = rules.compute_stats(deals, [{"ticket": 10, "identifier": 10, "symbol": "EURUSD", "magic": 77}], 77, DAY)
    assert stats.trades_today == 0  # el cupo sí es por día
    assert stats.last_entry_time == yesterday_2350  # el enfriamiento no
    cfg = RiskConfig(auto=AutoConfig(enabled=True, cooldown_minutes=60))
    assert rules.cooldown_remaining(cfg, stats, DAY + 30) == pytest.approx(60 - 630 / 60)
    assert any("enfriamiento" in r for r in rules.global_entry_blockers(cfg, stats, DAY + 30))
    assert rules.cooldown_remaining(cfg, stats, yesterday_2350 + 3600) == 0


def test_last_entry_is_the_latest_of_the_whole_window():
    deals = [_deal(1, rules.DEAL_ENTRY_IN, DAY - 5000), _deal(2, rules.DEAL_ENTRY_IN, DAY + 100), _deal(3, rules.DEAL_ENTRY_IN, DAY - 100)]
    stats = rules.compute_stats(deals, [], 77, DAY)
    assert (stats.trades_today, stats.last_entry_time) == (1, DAY + 100)
    assert rules.compute_stats([_deal(1, rules.DEAL_ENTRY_IN, DAY + 5, magic=5)], [], 77, DAY).last_entry_time == 0  # ajena


@pytest.mark.parametrize("winner_first", [True, False])
def test_streak_order_within_the_same_second_follows_deal_ticket(winner_first):
    t = DAY + 3600
    win_ticket, loss_ticket = (500, 501) if winner_first else (501, 500)
    deals = [
        _deal(1, rules.DEAL_ENTRY_IN, DAY + 10, ticket=11), _deal(2, rules.DEAL_ENTRY_IN, DAY + 20, ticket=12),
        _deal(1, rules.DEAL_ENTRY_OUT, t, magic=0, profit=5.0, ticket=win_ticket),
        _deal(2, rules.DEAL_ENTRY_OUT, t, magic=0, profit=-50.0, ticket=loss_ticket),
    ]
    stats = rules.compute_stats(deals, [], 77, DAY)
    assert stats.closed_today == 2 and stats.wins_today == 1
    # la última en cerrarse (ticket mayor) decide: si fue la perdedora, la racha es 1
    assert stats.consecutive_losses == (1 if winner_first else 0)


# ---- límite de pérdida diaria ------------------------------------------------------------------------


def _acct(balance):
    return AccountSnapshot(login=1, trade_mode=0, balance=balance, equity=balance, currency="USD")


def test_account_funded_today_gets_a_real_daily_limit():
    cfg = RiskConfig()  # 3 %
    fresh = RiskState(balance_ops_today=10_000.0)
    assert daily_loss_limit(cfg, _acct(10_000.0), fresh) == pytest.approx(-300.0)
    assert not daily_loss_hit(cfg, _acct(10_000.0), fresh)
    losing = RiskState(balance_ops_today=10_000.0, realized_today=-350.0)
    assert daily_loss_limit(cfg, _acct(9_650.0), losing) == pytest.approx(-300.0)
    assert daily_loss_hit(cfg, _acct(9_650.0), losing)


def test_funded_today_with_float_residue_does_not_collapse_the_limit():
    cfg = RiskConfig()
    # 1024.13 - 24.13 - 1000.0 deja un residuo de 1e-13: no debe tomarse como balance inicial
    state = RiskState(balance_ops_today=1000.0, realized_today=24.13, floating_pnl=-29.13)
    assert daily_loss_limit(cfg, _acct(1024.13), state) == pytest.approx(-30.0)
    assert not daily_loss_hit(cfg, _acct(1024.13), state)


def test_normal_day_limit_is_unchanged():
    cfg = RiskConfig()
    state = RiskState(realized_today=-100.0, balance_ops_today=500.0)
    assert daily_loss_limit(cfg, _acct(10_400.0), state) == pytest.approx(-300.0)
