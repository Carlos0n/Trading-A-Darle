"""Lectura estricta de risk.toml: un valor del tipo equivocado se rechaza nombrando la clave, no se convierte en silencio."""

import re
import tomllib
from dataclasses import fields
from pathlib import Path

import pytest

from mt5_mcp import config
from mt5_mcp.config import AutoConfig, RiskConfig, load_config

REPO = Path(__file__).resolve().parents[1]

# Claves numéricas de risk.toml: (tabla, clave). La tabla "" es la parte general y "auto" la tabla [auto].
DECIMAL_KEYS = [("", "max_risk_pct_per_trade"), ("", "max_daily_loss_pct"), ("", "max_volume_per_order"), ("", "max_total_volume"),
                ("auto", "risk_pct"), ("auto", "min_rr")]
WHOLE_KEYS = [("", "max_open_positions"), ("", "min_sl_points"), ("", "max_spread_points"), ("", "max_slippage_points"), ("", "magic"),
              ("auto", "max_trades_per_day"), ("auto", "max_consecutive_losses"), ("auto", "cooldown_minutes"),
              ("auto", "max_quote_age_seconds"), ("auto", "magic")]
NOT_A_NUMBER = ["true", "false", '"1"', "[1]"]  # un interruptor, un número entre comillas y una lista


def _load(tmp_path: Path, text: str) -> RiskConfig:
    p = tmp_path / "risk.toml"
    p.write_text(text, encoding="utf-8")
    return load_config(p)


def _line(table: str, key: str, value: str) -> str:
    return f"[{table}]\n{key} = {value}\n" if table else f"{key} = {value}\n"


def _name(table: str, key: str) -> str:
    """La clave como debe salir en el error: con su tabla delante si no es de la parte general."""
    return re.escape(f"[{table}] {key}" if table else key)


def _read(cfg: RiskConfig, table: str, key: str):
    return getattr(getattr(cfg, table) if table else cfg, key)


def test_every_numeric_key_is_covered_here():
    # una clave numérica nueva en la configuración obliga a añadirla a estas listas (y a leerla con el lector estricto)
    found = {("" if cls is RiskConfig else "auto", f.name): type(f.default) for cls in (RiskConfig, AutoConfig) for f in fields(cls)}
    assert {k for k, t in found.items() if t is float} == set(DECIMAL_KEYS)
    assert {k for k, t in found.items() if t is int} == set(WHOLE_KEYS)


@pytest.mark.parametrize("table, key", DECIMAL_KEYS + WHOLE_KEYS)
@pytest.mark.parametrize("value", NOT_A_NUMBER)
def test_a_switch_or_a_quoted_number_is_not_a_number(tmp_path, table, key, value):
    shown = re.escape(value.replace('"', "'"))
    with pytest.raises(ValueError, match=rf"^{_name(table, key)} debe ser un número.*sin comillas.*\(se leyó {shown}\)$"):
        _load(tmp_path, _line(table, key, value))


@pytest.mark.parametrize("table, key", WHOLE_KEYS)
@pytest.mark.parametrize("value", ["1.5", "2.0", "-0.9", "nan", "inf"])
def test_whole_number_keys_do_not_take_decimals(tmp_path, table, key, value):
    # int() recortaba los decimales antes de comprobar el rango: cooldown_minutes = -0.9 se cargaba como 0
    with pytest.raises(ValueError, match=rf"^{_name(table, key)} debe ser un número entero, sin comillas ni decimales \(se leyó {re.escape(value)}\)$"):
        _load(tmp_path, _line(table, key, value))


@pytest.mark.parametrize("text, wrong", [
    ("max_volume_per_order = true\nmax_total_volume = 3.0\n", "max_volume_per_order"),  # se cargaba como 1.0 lotes (por defecto 0.10)
    ("max_daily_loss_pct = true\n", "max_daily_loss_pct"),                              # se cargaba como 1.0
    ("[auto]\nmin_rr = true\n", "[auto] min_rr"),                                       # se cargaba como 1.0 (por defecto 1.5)
    ("[auto]\nrisk_pct = true\n", "[auto] risk_pct"),                                   # se cargaba como 1.0 (por defecto 0.5)
    ("[auto]\ncooldown_minutes = false\n", "[auto] cooldown_minutes"),                  # se cargaba como 0 (por defecto 15)
    ('[auto]\nmax_trades_per_day = "7"\n', "[auto] max_trades_per_day"),                # se cargaba como 7
    ("[auto]\ncooldown_minutes = -0.9\n", "[auto] cooldown_minutes"),                   # se cargaba como 0
])
def test_the_values_that_used_to_load_in_silence(tmp_path, text, wrong):
    with pytest.raises(ValueError) as err:
        _load(tmp_path, text)
    assert str(err.value).startswith(wrong + " debe ser un número") and "se leyó" in str(err.value)


@pytest.mark.parametrize("table, key, value, expected", [
    ("", "max_risk_pct_per_trade", "1", 1.0),       # un entero vale para una clave decimal
    ("", "max_risk_pct_per_trade", "0.25", 0.25),
    ("", "max_daily_loss_pct", "5", 5.0),
    ("", "max_volume_per_order", "0.05", 0.05),
    ("", "max_total_volume", "2", 2.0),
    ("auto", "risk_pct", "1", 1.0),
    ("auto", "min_rr", "2", 2.0),
    ("", "max_open_positions", "1", 1),
    ("", "min_sl_points", "50", 50),
    ("", "max_spread_points", "30", 30),
    ("", "max_slippage_points", "0", 0),
    ("", "magic", "7", 7),
    ("auto", "max_trades_per_day", "7", 7),
    ("auto", "max_consecutive_losses", "3", 3),
    ("auto", "cooldown_minutes", "0", 0),
    ("auto", "max_quote_age_seconds", "30", 30),
    ("auto", "magic", "4242", 4242),
])
def test_plain_numbers_load_with_their_type(tmp_path, table, key, value, expected):
    got = _read(_load(tmp_path, _line(table, key, value)), table, key)
    assert got == expected and type(got) is type(expected)
    # y solo cambia esa clave
    other = _load(tmp_path, _line(table, key, value))
    assert all(_read(other, t, k) == _read(RiskConfig(), t, k) for t, k in DECIMAL_KEYS + WHOLE_KEYS if (t, k) != (table, key))


def test_a_whole_number_too_big_for_a_decimal_is_explained(tmp_path):
    with pytest.raises(ValueError, match=r"^max_total_volume debe ser un número, sin comillas"):
        _load(tmp_path, "max_total_volume = 1" + "0" * 400 + "\n")


@pytest.mark.parametrize("key", ["max_volume_per_order", "max_total_volume"])
@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_volume_caps_reject_nan_and_infinity(tmp_path, key, value):
    # con nan ninguna comparación "volumen > tope" se cumple: el tope desaparecía sin avisar
    with pytest.raises(ValueError, match=rf"risk\.toml inválido: .*{key} debe ser"):
        _load(tmp_path, f"{key} = {value}\n")
    assert any(p.startswith(f"{key} debe ser") for p in RiskConfig(**{key: float(value)}).validate())


@pytest.mark.parametrize("key", ["max_risk_pct_per_trade", "max_daily_loss_pct"])
@pytest.mark.parametrize("value", ["nan", "inf"])
def test_percentage_limits_reject_nan_and_infinity(tmp_path, key, value):
    with pytest.raises(ValueError, match=rf"{key} debe estar entre"):
        _load(tmp_path, f"{key} = {value}\n")


def test_volume_caps_still_load_and_are_compared_with_each_other(tmp_path):
    cfg = _load(tmp_path, "max_volume_per_order = 0.5\nmax_total_volume = 0.5\n")
    assert (cfg.max_volume_per_order, cfg.max_total_volume) == (0.5, 0.5)
    for text in ("max_volume_per_order = 0\n", "max_volume_per_order = -0.1\n"):
        with pytest.raises(ValueError, match="max_volume_per_order debe ser > 0"):
            _load(tmp_path, text)
    with pytest.raises(ValueError, match="max_total_volume debe ser >= max_volume_per_order"):
        _load(tmp_path, "max_volume_per_order = 0.5\nmax_total_volume = 0.4\n")


# ---- trading_hours_utc, symbols y nombres de archivo ----------------------------------------------------


def test_trading_hours_load_as_two_whole_hours(tmp_path):
    assert _load(tmp_path, "trading_hours_utc = [7, 20]\n").trading_hours_utc == (7, 20)
    assert _load(tmp_path, "trading_hours_utc = [22, 6]\n").trading_hours_utc == (22, 6)
    assert _load(tmp_path, "trading_hours_utc = []\n").trading_hours_utc is None
    assert _load(tmp_path, 'symbols = ["EURUSD"]\n').trading_hours_utc is None


@pytest.mark.parametrize("value", ["[7]", "[7, 20, 22]", "[7.5, 20]", "[7, 20.0]", '["7", "20"]', "[true, 20]", "[7, false]",
                                   '"7-20"', '"ab"', "7", "0", "true", "false", "[[7, 20]]"])
def test_trading_hours_of_another_shape_are_rejected(tmp_path, value):
    # antes `false` y `0` se leían como "sin límite de horario" y 7.5 se recortaba a 7
    with pytest.raises(ValueError, match=r"^trading_hours_utc debe ser \[inicio, fin\] con horas enteras, o lista vacía \(se leyó .+\)$"):
        _load(tmp_path, f"trading_hours_utc = {value}\n")


@pytest.mark.parametrize("value", ["[25, 3]", "[7, 30]", "[-1, 20]"])
def test_trading_hours_outside_the_day_are_rejected(tmp_path, value):
    with pytest.raises(ValueError, match="trading_hours_utc debe contener horas entre 0 y 24"):
        _load(tmp_path, f"trading_hours_utc = {value}\n")


def test_symbols_load_as_exact_names(tmp_path):
    assert _load(tmp_path, 'symbols = ["EURUSD.m", " xauusd ", "", " "]\n').symbols == ("EURUSD.m", "xauusd")
    assert _load(tmp_path, "symbols = []\n").symbols == ()
    assert _load(tmp_path, "magic = 7\n").symbols == ()


@pytest.mark.parametrize("value", ['"EURUSD"', "[1, 2]", '["EURUSD", 5]', '["EURUSD", true]', "true", "1", '[["EURUSD"]]', '{ a = "EURUSD" }'])
def test_symbols_must_be_a_list_of_names(tmp_path, value):
    # sin corchetes, "EURUSD" se leía letra a letra y la lista blanca quedaba en E, U, R, U, S, D
    with pytest.raises(ValueError, match=r"^symbols debe ser una lista de nombres entre comillas.*\(se leyó .+\)$"):
        _load(tmp_path, f"symbols = {value}\n")


def test_file_names_load_as_written(tmp_path):
    cfg = _load(tmp_path, 'halt_file = "PARADA"\njournal_file = "journal/otro.jsonl"\n')
    assert (cfg.halt_file, cfg.journal_file) == ("PARADA", "journal/otro.jsonl")


@pytest.mark.parametrize("key", ["halt_file", "journal_file"])
@pytest.mark.parametrize("value", ["true", "false", "5", '["HALT"]', '""', '" "'])
def test_file_names_must_be_text(tmp_path, key, value):
    # `halt_file = true` creaba un archivo llamado "True"; con "" el archivo de parada era la propia carpeta del proyecto
    with pytest.raises(ValueError, match=rf"^{key} debe ser un nombre de archivo entre comillas \(se leyó .+\)$"):
        _load(tmp_path, f"{key} = {value}\n")


# ---- lo que ya estaba escrito tiene que seguir cargando -------------------------------------------------


def test_repo_risk_toml_loads_with_the_values_it_has_written():
    raw = tomllib.loads((REPO / "risk.toml").read_text(encoding="utf-8"))
    cfg = load_config(REPO / "risk.toml")
    auto = raw.pop("auto", {})
    for key, value in raw.items():
        got = getattr(cfg, key)
        if key == "trading_hours_utc":
            assert got == (tuple(value) or None), key
        elif key == "symbols":
            assert list(got) == value, key
        else:
            assert got == value and type(got) is type(getattr(RiskConfig(), key)), key
    assert all(getattr(cfg.auto, key) == value for key, value in auto.items())
    if "enabled" not in auto:
        assert cfg.auto.enabled is False  # sin tabla [auto], el modo automático queda apagado


def test_examples_written_in_the_repo_risk_toml_comments_load(tmp_path):
    # los "p.ej." de los comentarios de risk.toml
    cfg = _load(tmp_path, 'max_spread_points = 30\ntrading_hours_utc = [7, 20]\nrequire_tp = true\nsymbols = ["EURUSD.m"]\n')
    assert (cfg.max_spread_points, cfg.trading_hours_utc, cfg.require_tp, cfg.symbols) == (30, (7, 20), True, ("EURUSD.m",))


def test_readme_examples_load(tmp_path):
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```toml\n(.*?)```", readme, flags=re.S)
    assert blocks, "el README ya no trae ningún ejemplo de risk.toml"
    for block in blocks:
        text = "\n".join(line.strip() for line in block.splitlines()) + "\n"
        _load(tmp_path, text)  # solo
        _load(tmp_path, 'symbols = ["EURUSD"]\nmax_open_positions = 3\n\n' + text)  # y añadido al final de un archivo
    # la tabla de claves de [auto]: los valores "por defecto" documentados cargan y son los del código
    rows = [(k, v) for k, v in re.findall(r"^\|\s*`(\w+)`\s*\|\s*([^|]+?)\s*\|", readme, flags=re.M) if k in AutoConfig.__dataclass_fields__]
    assert rows, "el README ya no trae la tabla de claves de [auto]"
    cfg = _load(tmp_path, "[auto]\n" + "".join(f"{k} = {v}\n" for k, v in rows))
    assert cfg.auto == AutoConfig()
    for k, v in rows:  # y cada una por separado
        assert _load(tmp_path, f"[auto]\n{k} = {v}\n").auto == AutoConfig(), k


def test_a_missing_or_empty_file_gives_the_defaults(tmp_path):
    assert load_config(tmp_path / "no-existe.toml") == RiskConfig()
    assert _load(tmp_path, "") == RiskConfig()
    assert RiskConfig().validate() == []


# ---- raíz del proyecto ----------------------------------------------------------------------------------


def test_project_root_follows_claude_code_then_the_scripts_variable(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    package_parent = Path(config.__file__).resolve().parents[1]
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("MT5_MCP_ROOT", raising=False)
    assert config._project_root() == package_parent
    monkeypatch.setenv("MT5_MCP_ROOT", str(b))
    assert config._project_root() == b.resolve()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(a))  # lo que define Claude Code manda sobre MT5_MCP_ROOT
    assert config._project_root() == a.resolve()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "no-existe"))  # y MT5_MCP_ROOT no lo sustituye si está mal
    assert config._project_root() == package_parent
    monkeypatch.delenv("CLAUDE_PROJECT_DIR")
    monkeypatch.setenv("MT5_MCP_ROOT", str(tmp_path / "no-existe"))
    assert config._project_root() == package_parent
