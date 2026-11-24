"""Reglas puras de configuración, estadísticas del bot y límite diario (casos de la segunda revisión)."""

from dataclasses import replace
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


@pytest.mark.parametrize("start", [0.0, 0.004, 0.01, 0.07, 0.16, -0.5])
def test_cents_at_the_start_of_a_funded_day_are_not_the_base_of_the_limit(start):
    # Lo que deja un stop-out (unos céntimos, o un saldo negativo) y un depósito de 10.000 hoy. El 3 % de esa base no
    # llega a un céntimo: el límite saldría -0.00 y un P&L de 0 ya lo alcanzaría. La base es el balance tras el depósito.
    cfg = RiskConfig()  # 3 %
    state = RiskState(balance_ops_today=10_000.0)
    acct = _acct(start + 10_000.0)
    limit = daily_loss_limit(cfg, acct, state)
    assert limit == pytest.approx(-(start + 10_000.0) * 0.03) and round(limit, 2) == pytest.approx(-300.0, abs=0.015)
    assert not daily_loss_hit(cfg, acct, state)
    losing = RiskState(balance_ops_today=10_000.0, realized_today=-301.0)  # y una pérdida de verdad sí lo alcanza
    assert daily_loss_hit(cfg, _acct(start + 10_000.0 - 301.0), losing)


@pytest.mark.parametrize("start, limit", [(0.17, -0.01), (0.50, -0.01), (5.00, -0.15)])
def test_a_small_balance_whose_limit_reaches_a_cent_keeps_its_own_limit(start, limit):
    # Desde 0,17 el 3 % ya da al menos un céntimo: es la regla tal cual, el 3 % del balance con que empezó el día.
    cfg = RiskConfig()
    state = RiskState(balance_ops_today=10_000.0)
    acct = _acct(start + 10_000.0)
    assert daily_loss_limit(cfg, acct, state) == pytest.approx(-start * 0.03) and round(daily_loss_limit(cfg, acct, state), 2) == limit
    assert not daily_loss_hit(cfg, acct, state)
    losing = RiskState(balance_ops_today=10_000.0, realized_today=limit)
    assert daily_loss_hit(cfg, _acct(start + 10_000.0 + limit), losing)


def test_normal_day_limit_is_unchanged():
    cfg = RiskConfig()
    state = RiskState(realized_today=-100.0, balance_ops_today=500.0)
    assert daily_loss_limit(cfg, _acct(10_400.0), state) == pytest.approx(-300.0)
    assert daily_loss_limit(cfg, _acct(10_000.0), RiskState()) == pytest.approx(-300.0)
    assert daily_loss_limit(cfg, _acct(5.0), RiskState()) == pytest.approx(-0.15)  # cuenta pequeña sin depósito: su 3 %


# ---- tabla [auto]: todas las claves se leen y todos los rangos se comprueban -----------------------------

# Un valor distinto del de por defecto para cada clave de [auto].
AUTO_VALUES = dict(enabled=True, allow_live=True, risk_pct=0.3, max_trades_per_day=7, max_consecutive_losses=5, cooldown_minutes=45,
                   min_rr=2.5, one_position_per_symbol=False, max_quote_age_seconds=20, magic=4242)


def _auto_table(values: dict) -> str:
    return "[auto]\n" + "".join(f"{key} = {str(value).lower() if isinstance(value, bool) else value}\n" for key, value in values.items())


def test_every_auto_key_is_read_from_the_toml(tmp_path):
    default = AutoConfig()
    assert set(AUTO_VALUES) == set(AutoConfig.__dataclass_fields__)  # una clave nueva en [auto] obliga a añadirla aquí
    assert all(getattr(default, key) != value for key, value in AUTO_VALUES.items())
    cfg = load_config(_toml(tmp_path, _auto_table(AUTO_VALUES)))
    assert cfg.auto == AutoConfig(**AUTO_VALUES)
    assert cfg.auto_magic == 4242 and cfg.auto_risk_pct == 0.3
    for key, value in AUTO_VALUES.items():  # y de una en una: las demás se quedan en su valor por defecto
        assert load_config(_toml(tmp_path, _auto_table({key: value}))).auto == replace(default, **{key: value}), key


@pytest.mark.parametrize("key, lowest, highest, below, above", [
    ("risk_pct", 0.01, 5, 0, 5.01),
    ("max_trades_per_day", 1, 50, 0, 51),
    ("max_consecutive_losses", 1, 20, 0, 21),
    ("cooldown_minutes", 0, 1440, -1, 1441),
    ("min_rr", 0.5, 10, 0.49, 10.01),
    ("max_quote_age_seconds", 5, 600, 4, 601),
])
def test_auto_limits_only_load_inside_their_range(tmp_path, key, lowest, highest, below, above):
    for ok in (lowest, highest):
        assert getattr(load_config(_toml(tmp_path, f"[auto]\n{key} = {ok}\n")).auto, key) == ok
    for bad in (below, above):
        with pytest.raises(ValueError, match=rf"auto\.{key} debe estar entre"):
            load_config(_toml(tmp_path, f"[auto]\n{key} = {bad}\n"))
        assert any(p.startswith(f"auto.{key} ") for p in RiskConfig(auto=AutoConfig(**{key: bad})).validate())


@pytest.mark.parametrize("key", ["risk_pct", "min_rr"])
@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_auto_limits_reject_nan_and_infinity(tmp_path, key, value):
    # con min_rr = nan ninguna comparación "ratio < mínimo" se cumple: el mínimo dejaría de aplicarse sin avisar
    with pytest.raises(ValueError, match=rf"auto\.{key} debe estar entre"):
        load_config(_toml(tmp_path, f"[auto]\n{key} = {value}\n"))


def test_auto_magic_cannot_be_negative(tmp_path):
    with pytest.raises(ValueError, match=r"auto\.magic debe ser >= 0"):
        load_config(_toml(tmp_path, "[auto]\nmagic = -1\n"))
    assert load_config(_toml(tmp_path, "magic = 7\n[auto]\nmagic = 0\n")).auto_magic == 8  # 0 significa "el general + 1"


@pytest.mark.parametrize("text", ["auto = true\n", 'auto = "enabled"\n', "auto = 1\n", "auto = [true]\n", "[[auto]]\nenabled = true\n"])
def test_auto_must_be_a_table(tmp_path, text):
    # `auto = true` no activa nada: el archivo tiene que rechazarse, no cargarse en silencio con el modo apagado
    with pytest.raises(ValueError, match=r"\[auto\] debe ser una tabla"):
        load_config(_toml(tmp_path, text))


# ---- estadísticas del bot: orden de los cierres, tipos de cierre y resultado neto -------------------------

IN, OUT = rules.DEAL_ENTRY_IN, rules.DEAL_ENTRY_OUT


def test_streak_follows_the_order_of_closing_when_positions_overlap():
    first_opened_loses_last = [
        _deal(1, IN, DAY + 10),                                               # A abre primero...
        _deal(2, IN, DAY + 20), _deal(2, OUT, DAY + 30, magic=0, profit=15.0),   # B abre después y gana
        _deal(1, OUT, DAY + 40, magic=0, profit=-10.0),                        # ...y A cierra la última, perdiendo
    ]
    stats = rules.compute_stats(first_opened_loses_last, [], 77, DAY)
    assert (stats.closed_today, stats.wins_today, stats.consecutive_losses) == (2, 1, 1)
    first_opened_wins_last = [
        _deal(1, IN, DAY + 10),
        _deal(2, IN, DAY + 20), _deal(2, OUT, DAY + 30, magic=0, profit=-10.0),
        _deal(1, OUT, DAY + 40, magic=0, profit=15.0),                         # el último cierre es una ganancia: no hay racha
    ]
    stats = rules.compute_stats(first_opened_wins_last, [], 77, DAY)
    assert (stats.closed_today, stats.wins_today, stats.consecutive_losses) == (2, 1, 0)


def test_close_by_an_opposite_position_counts_as_a_close():
    deals = [_deal(3, IN, DAY + 50), _deal(3, rules.DEAL_ENTRY_OUT_BY, DAY + 60, magic=0, profit=-5.0)]
    stats = rules.compute_stats(deals, [], 77, DAY)
    assert (stats.closed_today, stats.consecutive_losses, stats.realized_today) == (1, 1, -5.0)


def test_midnight_itself_belongs_to_the_new_day():
    deals = [
        _deal(1, IN, DAY - 100), _deal(1, OUT, DAY - 1, magic=0, profit=-30.0),  # cerrada a las 23:59:59: es de ayer
        _deal(2, IN, DAY - 50), _deal(2, OUT, DAY, magic=0, profit=-20.0),       # cerrada a las 00:00:00: es de hoy
        _deal(3, IN, DAY),                                                       # abierta a las 00:00:00: cuenta en el cupo de hoy
    ]
    stats = rules.compute_stats(deals, [{"ticket": 3, "identifier": 3, "symbol": "EURUSD", "magic": 77}], 77, DAY)
    assert (stats.trades_today, stats.closed_today, stats.consecutive_losses, stats.realized_today) == (1, 1, 1, -20.0)


def test_a_close_at_zero_is_neither_a_win_nor_a_loss():
    deals = [
        _deal(1, IN, DAY + 1), _deal(1, OUT, DAY + 2, magic=0, profit=-10.0),
        _deal(2, IN, DAY + 3), _deal(2, OUT, DAY + 4, magic=0, profit=0.0),
    ]
    stats = rules.compute_stats(deals, [], 77, DAY)
    assert (stats.closed_today, stats.wins_today, stats.consecutive_losses) == (2, 0, 0)  # el cierre a cero corta la racha


def test_net_result_of_a_trade_adds_commission_swap_and_fee():
    assert rules.deal_net({"profit": 12.0, "commission": -3.0, "swap": -7.5, "fee": -2.0}) == -0.5
    # gana 3 por precio y paga 4,25 de swap por pasar la noche: es una pérdida y cuenta en la racha
    overnight = [_deal(1, IN, DAY - 3600), {**_deal(1, OUT, DAY + 60, magic=0, profit=3.0), "swap": -4.25}]
    stats = rules.compute_stats(overnight, [], 77, DAY)
    assert (stats.realized_today, stats.wins_today, stats.consecutive_losses) == (-1.25, 0, 1)
