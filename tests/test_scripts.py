"""Pruebas de los scripts del usuario: scripts/kill_switch.py (parada manual) y scripts/check_connection.py (diagnóstico).

Se cargan como módulos desde la carpeta scripts y se llama a su main(). La raíz del proyecto y risk.toml van siempre a
una carpeta temporal y el terminal es el doble de tests/fake_mt5.py: ninguna prueba toca el HALT del repositorio.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from mt5_mcp import config
from mt5_mcp.broker import BrokerError, MT5Broker
from mt5_mcp.engine import Engine

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
BAD_TOML = 'symbols = ["EURUSD"]\n[auto]\nenabled = "true"\n'  # el interruptor entre comillas: risk.toml no carga
BROKEN_TOML = 'symbols = ["EURUSD"\n'  # ni siquiera es TOML
NO_PACKAGE = "No se pudo importar el paquete MetaTrader5. Solo existe para Windows x64"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"script_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stop_files() -> dict[str, bytes | bool | None]:
    """Archivos de parada de la raíz del repositorio (los nombres que usan estas pruebas): contenido, True si es una
    carpeta, None si no existe."""
    return {name: (REPO / name).read_bytes() if (REPO / name).is_file() else (REPO / name).exists() or None
            for name in ("HALT", "PARADA")}


@pytest.fixture(autouse=True)
def repo_root_untouched():
    """Ninguna prueba crea ni cambia un archivo de parada en la raíz del repositorio."""
    before = _stop_files()
    yield
    assert _stop_files() == before
    if before["HALT"] is None:
        assert not (REPO / "HALT").exists()


@pytest.fixture
def env(fake, tmp_path, monkeypatch):
    """Los dos scripts cargados, con la raíz del proyecto y risk.toml en tmp_path y el terminal en el doble.

    `env.terminal = None` simula que falta el paquete MetaTrader5 (MT5Broker() lanza BrokerError al crearse);
    `fake.fail_init = True`, que el terminal no responde (initialize() falla al primer uso).
    kill_switch.py usa su propio Engine, sin sleep=: si la parada esperase a la hora del servidor, la guarda de
    conftest.py haría fallar la prueba.
    """
    monkeypatch.setattr(sys, "path", list(sys.path))  # los scripts añaden la raíz del repo a sys.path al cargarse
    kill_switch, check_connection = _load("kill_switch"), _load("check_connection")

    class Env:
        root = tmp_path
        toml = tmp_path / "risk.toml"
        terminal = fake
        brokers: list[dict] = []
        engines: list[Engine] = []
        ks = kill_switch
        cc = check_connection

        @staticmethod
        def journal() -> list[dict]:
            path = tmp_path / "journal" / "trades.jsonl"
            return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def broker(*args, **kwargs):
        Env.brokers.append(kwargs)
        if Env.terminal is None:
            raise BrokerError(NO_PACKAGE)
        return MT5Broker(*args, module=Env.terminal, **kwargs)

    def engine(b, **kwargs):  # el diagnóstico no pasa sleep=: aquí la espera al reloj del servidor no duerme de verdad
        eng = Engine(b, sleep=fake.sleep, **kwargs)
        eng._now = fake.utcnow
        Env.engines.append(eng)
        return eng

    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", Env.toml)
    monkeypatch.setattr("mt5_mcp.engine.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(kill_switch, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(kill_switch, "MT5Broker", broker)
    monkeypatch.setattr(check_connection, "MT5Broker", broker)
    monkeypatch.setattr(check_connection, "Engine", engine)
    return Env


# ---- kill_switch.py ---------------------------------------------------------------------------------------------


def test_kill_switch_creates_halt_without_contacting_metatrader(env, fake, capsys):
    env.toml.write_text('symbols = ["EURUSD"]\n', encoding="utf-8")
    fake.add_position("EURUSD", "buy", 0.1, 1.0990, sl=1.0950)
    assert env.ks.main(["--reason", "pánico"]) == 0
    halt = env.root / "HALT"
    assert halt.read_text(encoding="utf-8") == "pánico\n"
    out = capsys.readouterr().out
    assert f"HALT creado en {halt}" in out and "No se abrirán órdenes nuevas" in out and "AVISO" not in out
    assert env.brokers == [] and not fake.initialized and not fake.sent and len(fake.positions_get()) == 1
    assert env.ks.main([]) == 0  # una segunda parada añade su línea, no borra la anterior
    assert halt.read_text(encoding="utf-8") == "pánico\nkill switch manual\n"


def test_kill_switch_without_the_metatrader5_package_still_creates_halt(env, capsys):
    env.terminal = None
    assert env.ks.main(["--reason", "sin terminal"]) == 0  # sin --close-all no hace falta MetaTrader 5
    assert (env.root / "HALT").read_text(encoding="utf-8") == "sin terminal\n"
    assert "HALT creado en" in capsys.readouterr().out


@pytest.mark.parametrize("text", [BAD_TOML, BROKEN_TOML], ids=["valor-invalido", "toml-roto"])
def test_kill_switch_with_an_invalid_risk_toml_still_creates_halt(env, fake, capsys, text):
    env.toml.write_text(text, encoding="utf-8")
    assert env.ks.main(["--reason", "pánico"]) == 0
    assert (env.root / "HALT").read_text(encoding="utf-8") == "pánico\n"
    out = capsys.readouterr().out
    assert "AVISO: risk.toml no es válido" in out and "el archivo de parada es HALT" in out and "Corrige risk.toml" in out
    if text is BAD_TOML:
        assert "sin comillas" in out  # el motivo real, para que el usuario sepa qué corregir
    assert f"HALT creado en {env.root / 'HALT'}" in out
    assert sorted(p.name for p in env.root.iterdir()) == ["HALT", "risk.toml"] and env.brokers == []


def test_kill_switch_uses_the_halt_file_named_in_risk_toml(env, fake, capsys):
    env.toml.write_text('symbols = ["EURUSD"]\nhalt_file = "PARADA"\n', encoding="utf-8")
    assert env.ks.main(["--reason", "uno"]) == 0
    fake.add_position("EURUSD", "buy", 0.1, 1.0990, sl=1.0950)
    assert env.ks.main(["--close-all", "--reason", "dos"]) == 0
    assert not (env.root / "HALT").exists()
    lines = (env.root / "PARADA").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "uno" and lines[1].endswith(" dos") and len(lines) == 2
    out = capsys.readouterr().out
    assert f"HALT creado en {env.root / 'PARADA'}" in out and "AVISO" not in out
    assert fake.positions_get() == ()


def test_kill_switch_close_all_closes_every_position(env, fake, capsys):
    env.toml.write_text('symbols = ["EURUSD"]\nmagic = 4242\n', encoding="utf-8")
    fake.add_symbol("GBPUSD")
    fake.set_tick("GBPUSD", 1.30000, 1.30010)
    mine = fake.add_position("EURUSD", "buy", 0.1, 1.0990, sl=1.0950, magic=4242)
    external = fake.add_position("GBPUSD", "sell", 0.2, 1.3050, sl=1.3100)  # abierta a mano en el terminal: también se cierra
    assert env.ks.main(["--close-all", "--reason", "pánico"]) == 0
    assert fake.positions_get() == ()
    assert [(r["position"], r["volume"], r["magic"]) for r in fake.sent] == [(mine, 0.1, 4242), (external, 0.2, 4242)]
    out = capsys.readouterr().out
    assert f"ticket {mine}: cerrada" in out and f"ticket {external}: cerrada" in out
    assert "error" not in out and "Revisa el terminal" not in out
    halt = env.root / "HALT"
    assert f"HALT creado en {halt}" in out and halt.read_text(encoding="utf-8").strip().endswith("pánico")
    events = [e["event"] for e in env.journal()]
    assert events.count("position_closed") == 2 and events[-1] == "halt"
    assert env.journal()[-1]["close_all"] is True and env.journal()[-1]["errors"] == []


def test_kill_switch_close_all_with_an_invalid_risk_toml(env, fake, capsys):
    env.toml.write_text(BAD_TOML, encoding="utf-8")
    first = fake.add_position("EURUSD", "buy", 0.1, 1.0990, sl=1.0950, magic=20261023)
    second = fake.add_position("EURUSD", "sell", 0.1, 1.1020, sl=1.1050)
    assert env.ks.main(["--close-all"]) == 0  # la parada y el cierre no dependen de que risk.toml sea válido
    assert fake.positions_get() == () and len(fake.sent) == 2
    out = capsys.readouterr().out
    assert "AVISO: risk.toml no es válido" in out and "sin comillas" in out and "el archivo de parada es HALT" in out
    assert f"ticket {first}: cerrada" in out and f"ticket {second}: cerrada" in out
    assert (env.root / "HALT").read_text(encoding="utf-8").strip().endswith("kill switch manual")


def test_kill_switch_close_all_reports_a_position_it_could_not_close(env, fake, capsys):
    env.toml.write_text('symbols = ["EURUSD"]\n', encoding="utf-8")
    ticket = fake.add_position("EURUSD", "buy", 0.1, 1.0990, sl=1.0950)
    fake.send_retcode = 10006  # el broker rechaza el cierre
    assert env.ks.main(["--close-all"]) == 1
    out = capsys.readouterr().out
    assert f"ticket {ticket}: NO CERRADA DEL TODO" in out and "Revisa el terminal: alguna posición puede seguir abierta" in out
    assert len(fake.positions_get()) == 1 and (env.root / "HALT").exists()


def test_kill_switch_close_all_with_metatrader_not_answering(env, fake, capsys):
    env.toml.write_text('symbols = ["EURUSD"]\n', encoding="utf-8")
    fake.add_position("EURUSD", "buy", 0.1, 1.0990, sl=1.0950)
    fake.fail_init = True  # el terminal está cerrado o no responde
    assert env.ks.main(["--close-all", "--reason", "pánico"]) == 1
    halt = env.root / "HALT"
    assert halt.read_text(encoding="utf-8").strip().endswith("pánico")  # la parada vale aunque no se pudo cerrar nada
    out = capsys.readouterr().out
    assert f"HALT creado en {halt}" in out
    assert "no se pudieron leer las posiciones para cerrarlas: initialize() falló" in out
    assert "Revisa el terminal: alguna posición puede seguir abierta" in out
    assert "cerrada" not in out and not fake.sent and len(fake.positions_get()) == 1


def test_kill_switch_close_all_without_the_metatrader5_package(env, capsys):
    env.toml.write_text('symbols = ["EURUSD"]\n', encoding="utf-8")
    env.terminal = None
    assert env.ks.main(["--close-all", "--reason", "pánico"]) == 1
    halt = env.root / "HALT"
    assert halt.read_text(encoding="utf-8") == f"pánico (sin conexión: {NO_PACKAGE})\n"
    out = capsys.readouterr().out
    assert f"HALT creado en {halt}." in out and "ERROR" not in out
    assert f"No se pudo conectar con MT5 para cerrar posiciones: {NO_PACKAGE}" in out
    assert "Cierra las posiciones a mano en el terminal" in out


@pytest.mark.parametrize("close_all", [False, True], ids=["solo-parada", "close-all"])
def test_kill_switch_says_so_when_the_halt_file_cannot_be_created(env, fake, capsys, close_all):
    (env.root / "sub").write_text("un archivo, no una carpeta", encoding="utf-8")  # la carpeta del archivo de parada no puede crearse
    env.toml.write_text('symbols = ["EURUSD"]\nhalt_file = "sub/HALT"\n', encoding="utf-8")
    fake.add_position("EURUSD", "buy", 0.1, 1.0990, sl=1.0950)
    assert env.ks.main(["--close-all"] if close_all else []) == 1
    out = capsys.readouterr().out
    halt = env.root / "sub" / "HALT"
    assert "HALT creado" not in out and "Traceback" not in out and not halt.exists()
    assert f"ERROR: no se pudo crear el archivo de parada {halt}" in out and "No queda nada detenido" in out
    assert "apaga el botón Algo Trading" in out
    if close_all:  # cerrar reduce riesgo: se hace igual, y no se dice que quedan posiciones cuando no queda ninguna
        assert fake.positions_get() == () and "ticket 1000: cerrada" in out and "Revisa el terminal" not in out
    else:
        assert f"{halt} (" in out and len(fake.positions_get()) == 1 and not fake.sent  # con el motivo del sistema


def test_kill_switch_with_an_existing_folder_named_halt(env, capsys):
    (env.root / "HALT").mkdir()  # no se puede escribir en ella, pero existe: el servidor y el bucle la ven como parada
    assert env.ks.main([]) == 0
    assert "HALT creado en" in capsys.readouterr().out and (env.root / "HALT").is_dir()


def test_kill_switch_without_the_package_and_a_halt_file_that_cannot_be_created(env, capsys):
    (env.root / "sub").write_text("un archivo", encoding="utf-8")
    env.toml.write_text('halt_file = "sub/HALT"\n', encoding="utf-8")
    env.terminal = None
    assert env.ks.main(["--close-all"]) == 1
    out = capsys.readouterr().out
    assert "HALT creado" not in out and "ERROR: no se pudo crear el archivo de parada" in out
    assert NO_PACKAGE in out and "Cierra las posiciones a mano en el terminal" in out


# ---- check_connection.py ----------------------------------------------------------------------------------------


def test_check_connection_with_the_market_open(env, fake, capsys):
    env.toml.write_text('symbols = ["EURUSD"]\n', encoding="utf-8")
    fake.server_offset = 7200  # servidor en UTC+2
    assert env.cc.main() == 0
    out = capsys.readouterr().out
    assert "Cuenta            : 12345678 @ Demo-Server" in out and "DEMO (demo)" in out
    assert "Balance           : 10000.0 USD | equity: 10000.0" in out
    assert "Algo Trading      : ACTIVADO" in out and "API Python en MT5 : permitida" in out
    assert "Kill switch       : inactivo" in out
    assert "Hora del servidor : conocida (UTC+2 h)" in out and "DESCONOCIDA" not in out
    assert "Puede abrir       : SI" in out and "bloqueo:" not in out
    assert "EURUSD: bid 1.1 ask 1.1001 spread 10.0 pts\n" in out  # cotización reciente: sin aviso
    assert not fake.sent and not fake.checked and not fake.initialized  # no envía órdenes y se desconecta al acabar
    assert len(env.brokers) == 1 and len(env.engines) == 1 and env.engines[0].root == env.root
    assert not (env.root / "HALT").exists() and not (env.root / "journal").exists()


def test_check_connection_with_the_market_closed(env, fake, capsys):
    env.toml.write_text('symbols = ["EURUSD"]\n', encoding="utf-8")
    fake.freeze_ticks(age=3600)  # mercado cerrado: ningún tick se mueve
    assert env.cc.main() == 0
    out = capsys.readouterr().out
    assert "Hora del servidor : DESCONOCIDA: no llega ninguna cotización nueva (mercado cerrado" in out
    assert "Sin ella" not in out.split("Hora del servidor : ")[1].splitlines()[0]  # solo el motivo, sin la coletilla
    assert "Si el mercado está cerrado es lo esperado" in out and "conocida (UTC" not in out
    assert "Puede abrir       : NO" in out and "bloqueo: hora del servidor desconocida" in out
    assert "EURUSD: bid 1.1 ask 1.1001 spread 10.0 pts (antigüedad desconocida: falta la hora del servidor)" in out
    assert not fake.sent and not fake.initialized


def test_check_connection_shows_an_active_halt(env, fake, capsys):
    env.toml.write_text('symbols = ["EURUSD"]\n', encoding="utf-8")
    (env.root / "HALT").write_text("parada\n", encoding="utf-8")
    assert env.cc.main() == 0
    out = capsys.readouterr().out
    assert "Kill switch       : ACTIVO" in out and "Puede abrir       : NO" in out and "kill switch" in out.split("Puede abrir")[1]
    assert (env.root / "HALT").read_text(encoding="utf-8") == "parada\n"  # el diagnóstico no toca la parada


@pytest.mark.parametrize("text", [BAD_TOML, BROKEN_TOML], ids=["valor-invalido", "toml-roto"])
def test_check_connection_with_an_invalid_risk_toml(env, fake, capsys, text):
    env.toml.write_text(text, encoding="utf-8")
    assert env.cc.main() == 1
    out = capsys.readouterr().out
    assert "ERROR: revisa risk.toml:" in out and "Traceback" not in out
    if text is BAD_TOML:
        assert "sin comillas" in out
    assert "Cuenta" not in out and not fake.sent


def test_check_connection_with_metatrader_not_answering(env, fake, capsys):
    env.toml.write_text('symbols = ["EURUSD"]\n', encoding="utf-8")
    fake.fail_init = True
    assert env.cc.main() == 1
    out = capsys.readouterr().out
    assert "ERROR: initialize() falló" in out and "Comprueba que MetaTrader 5 está abierto" in out and "Cuenta" not in out


def test_check_connection_without_the_metatrader5_package(env, capsys):
    env.toml.write_text('symbols = ["EURUSD"]\n', encoding="utf-8")
    env.terminal = None
    assert env.cc.main() == 1
    out = capsys.readouterr().out
    assert f"ERROR: {NO_PACKAGE}" in out and "Cuenta" not in out
