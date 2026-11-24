"""Bucle del modo automático: comando, lectura de eventos, comprobación previa y control del bucle."""

import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import WHY, FailingJournal, World
from fake_mt5 import FakeMT5

from mt5_mcp import autoloop, server
from mt5_mcp.broker import BrokerError, MT5Broker
from mt5_mcp.config import AutoConfig, RiskConfig
from mt5_mcp.engine import Engine
from mt5_mcp.journal import Journal

REPO = Path(__file__).resolve().parents[1]
FAKE = [sys.executable, str(REPO / "tests" / "fake_claude.py")]
T0 = 1_800_000_000.0 + 7 * 60  # reloj simulado del bucle: 7 minutos después de un cierre de vela de 15 y de 60 min
FIRST_WAIT = 8 * 60 + 20  # desde T0 hasta el siguiente cierre de 15 min más el margen de 20 s
VALID_TOML = 'symbols = ["EURUSD"]\n\n[auto]\nenabled = true\n'
ALGO_OFF = "AutoTrading está desactivado en el terminal MT5 (botón 'Algo Trading'); actívalo a mano"
LEFT_OPEN_1 = ". Queda 1 posición del bot abierta sin revisar; conserva el SL y el TP que tiene en el broker"
LEFT_OPEN_2 = ". Quedan 2 posiciones del bot abiertas sin revisar; conservan el SL y el TP que tienen en el broker"
LEFT_OPEN_NO_CLOCK = ". Queda 1 posición del bot abierta sin revisar mientras no se conozca la hora del servidor; conserva el SL y el TP que tiene en el broker"
CLOCK_BLOCKER = "hora del servidor desconocida: llegan cotizaciones, pero su hora no encaja con el reloj de este PC"


@pytest.fixture
def root(tmp_path):
    (tmp_path / "risk.toml").write_text(VALID_TOML, encoding="utf-8")
    (tmp_path / "auto_protocol.md").write_text("# Protocolo\nLlama a auto_status.\n", encoding="utf-8")
    (tmp_path / "strategy.md").write_text("# Estrategia\nCompra si H1 sube.\n", encoding="utf-8")
    return tmp_path


def args(*extra):
    return autoloop.parse_args(["--once", *extra])


class Lines(list):
    def __call__(self, text):
        self.append(text)

    def has(self, fragment):
        return any(fragment in line for line in self)

    def times(self, fragment):
        return sum(fragment in line for line in self)


class StubEngine:
    """Motor de mentira: `outcomes` son los resultados de auto_status ciclo a ciclo (el último se repite)."""

    def __init__(self, status=None, error=None, outcomes=None):
        self.outcomes = list(outcomes) if outcomes is not None else [error if error is not None else status]
        self.calls = self.shutdowns = 0
        self.b = self

    def shutdown(self):
        self.shutdowns += 1

    def auto_status(self):
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def status(**changes):
    base = {"enabled": True, "can_open": True, "can_manage_positions": True, "terminal_can_trade": True, "blockers": [],
            "open_auto_positions": [], "server_clock_known": True}
    base.update(changes)
    return base


def disclaims_profit(prompt):
    """El mensaje del ciclo dice, con esas palabras, que la estrategia no garantiza ganancias."""
    return "no garantiza ganancias" in " ".join(prompt.replace(">", " ").split()).lower()


# ---- piezas puras --------------------------------------------------------------------------------


def test_tool_lists_cover_every_server_tool_and_keep_manual_writes_out():
    tools = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert set(autoloop.ALLOWED_TOOLS) | set(autoloop.HIDDEN_TOOLS) == tools
    assert not set(autoloop.ALLOWED_TOOLS) & set(autoloop.HIDDEN_TOOLS)
    for manual in ("order_send", "close_position", "modify_position", "halt_trading"):
        assert manual in autoloop.HIDDEN_TOOLS and manual not in autoloop.ALLOWED_TOOLS


def test_claude_command_is_locked_down(tmp_path):
    cmd = autoloop.claude_command(["claude"], tmp_path / "mcp.json", "sonnet", 25)
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert "--strict-mcp-config" in cmd and cmd[cmd.index("--mcp-config") + 1] == str(tmp_path / "mcp.json")
    assert "--tools=" in cmd and "bypassPermissions" not in cmd and "--dangerously-skip-permissions" not in cmd
    allowed = cmd[cmd.index("--allowedTools") + 1:cmd.index("--disallowedTools")]
    hidden = cmd[cmd.index("--disallowedTools") + 1:cmd.index("--max-turns")]
    assert allowed == [f"mcp__mt5__{t}" for t in autoloop.ALLOWED_TOOLS] and "mcp__mt5__auto_order" in allowed
    assert "mcp__mt5__order_send" in hidden and "mcp__mt5__order_send" not in allowed
    assert all("*" not in t for t in allowed)  # lista explícita, sin comodines
    assert cmd[cmd.index("--max-turns") + 1] == "25" and cmd[-2:] == ["--model", "sonnet"]
    assert cmd[cmd.index("--output-format") + 1] == "stream-json" and "--verbose" in cmd
    assert "--model" not in autoloop.claude_command(["claude"], tmp_path / "mcp.json", None, 40)


def test_build_prompt_joins_protocol_and_strategy(root):
    prompt = autoloop.build_prompt(root)
    assert "UN ciclo" in prompt and "Llama a auto_status." in prompt and "Compra si H1 sube." in prompt
    assert prompt.index("PROTOCOLO") < prompt.index("ESTRATEGIA")
    (root / "strategy.md").unlink()
    with pytest.raises(FileNotFoundError, match="strategy.md"):
        autoloop.build_prompt(root)
    (root / "strategy.md").write_text("x", encoding="utf-8")
    (root / "auto_protocol.md").unlink()
    with pytest.raises(FileNotFoundError, match="auto_protocol.md"):
        autoloop.build_prompt(root)


def test_repo_protocol_and_strategy_exist_and_forbid_manual_tools():
    prompt = autoloop.build_prompt(REPO)
    assert "auto_order" in prompt and "auto_status" in prompt and "Nunca `order_send`" in prompt
    assert disclaims_profit(prompt)


def test_profit_disclaimer_check_rejects_a_promise(root):
    (root / "strategy.md").write_text("> Plantilla de ejemplo. No está validada y no\n> garantiza ganancias: puede perder.\n", encoding="utf-8")
    assert disclaims_profit(autoloop.build_prompt(root))
    (root / "strategy.md").write_text("No garantiza ganancias: puede perder.\n", encoding="utf-8")
    assert disclaims_profit(autoloop.build_prompt(root))
    (root / "strategy.md").write_text("Está validada y garantiza ganancias todas las semanas.\n", encoding="utf-8")
    assert not disclaims_profit(autoloop.build_prompt(root))


def test_write_mcp_config_has_no_secrets(root, monkeypatch):
    monkeypatch.setenv("MT5_PASSWORD", "secreto")
    monkeypatch.setenv("MT5_LOGIN", "123")
    monkeypatch.setenv("MT5_TERMINAL_PATH", "C:\\MT5\\terminal64.exe")
    path = autoloop.write_mcp_config(root, "C:\\repo\\.venv\\Scripts\\python.exe")
    text = path.read_text(encoding="utf-8")
    cfg = json.loads(text)["mcpServers"]["mt5"]
    assert cfg["command"] == "C:\\repo\\.venv\\Scripts\\python.exe" and cfg["args"] == ["-m", "mt5_mcp"]
    assert cfg["env"]["MT5_MCP_ROOT"] == str(root) and cfg["env"]["MT5_TERMINAL_PATH"].endswith("terminal64.exe")
    assert "secreto" not in text and "MT5_PASSWORD" not in text and "MT5_LOGIN" not in text
    assert path.parent.name == "journal"


def test_next_boundary_aligns_to_candle_close():
    base = 1_800_000_000 - (1_800_000_000 % 900)  # un múltiplo exacto de 15 minutos
    assert autoloop.next_boundary(base + 7 * 60, 15, 20) == base + 900 + 20
    assert autoloop.next_boundary(base + 5, 15, 20) == base + 20          # el margen de este cierre aún no pasó
    assert autoloop.next_boundary(base + 20, 15, 20) == base + 900 + 20
    assert autoloop.next_boundary(base + 30, 15, 20) == base + 900 + 20
    assert autoloop.next_boundary(base + 899, 15, 20) == base + 900 + 20
    assert autoloop.next_boundary(base + 61, 1, 5) == base + 65


# ---- lectura de eventos --------------------------------------------------------------------------


def test_run_cycle_reads_events_and_sends_prompt_by_stdin(root, monkeypatch, tmp_path):
    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    out = Lines()
    cmd = autoloop.claude_command(FAKE, root / "journal" / "mcp.json", None, 40)
    report, problems = autoloop.run_cycle(cmd, "MENSAJE con acentos: operación", root, 30, out)
    assert problems == []
    assert report.mcp_status == "connected" and report.finished and not report.is_error
    assert report.turns == 4 and report.cost == pytest.approx(0.0312)
    assert report.tool_calls == ["auto_status", "order_send", "auto_order"]
    assert report.denied == ["mcp__mt5__order_send"]
    assert out.has("Servidor mt5: connected") and out.has("Claude: Reviso el estado del bot.")
    assert out.has("→ auto_order") and out.has('"sent": true') and out.has("DENEGADO por permisos: mcp__mt5__order_send")
    assert out.has("línea que no es JSON") and out.has("aviso por stderr")
    seen = json.loads(record.read_text(encoding="utf-8"))
    assert seen["prompt"] == "MENSAJE con acentos: operación" and Path(seen["cwd"]) == root
    assert "-p" in seen["argv"] and "MENSAJE" not in " ".join(seen["argv"])


@pytest.mark.parametrize("mode,fragment", [
    ("mcp_failed", "no conectó"),
    ("max_turns", "código 1"),
    ("no_tools", "no consultó auto_status"),
])
def test_run_cycle_reports_problems(root, monkeypatch, mode, fragment):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    report, problems = autoloop.run_cycle(autoloop.claude_command(FAKE, root / "m.json", None, 40), "x", root, 30, Lines())
    assert any(fragment in p for p in problems), problems


def test_run_cycle_kills_a_hung_cycle(root, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "hang")
    started = time.monotonic()
    report, problems = autoloop.run_cycle(autoloop.claude_command(FAKE, root / "m.json", None, 40), "x", root, 2, Lines())
    elapsed = time.monotonic() - started
    assert elapsed < 20, "nadie mató el proceso colgado: el doble duerme 60 s y el ciclo esperó a que terminara solo"
    assert elapsed >= 1.5  # no se le mata antes del tiempo máximo
    assert any("tiempo máximo" in p for p in problems) and not report.finished
    assert not any("código" in p for p in problems)  # la muerte por tiempo máximo no se cuenta además como fallo de claude


def test_run_cycle_missing_executable(root):
    report, problems = autoloop.run_cycle(["no-existe-este-programa-xyz", "-p"], "x", root, 5, Lines())
    assert problems and "no se pudo lanzar" in problems[0]


def test_cycle_report_ignores_malformed_events():
    out = Lines()
    r = autoloop.CycleReport(out)
    for line in ("", "[1, 2]", '{"type": "assistant", "message": null}', '{"type": "user", "message": {"content": "texto"}}',
                 '{"type": "system", "subtype": "init"}', '{"type": "result", "num_turns": null}'):
        r.feed(line)
    assert r.finished and r.mcp_status is None and r.turns == 0
    assert any("no conectó" in p for p in r.problems(0, False))


def test_cycle_report_flags_error_result_and_missing_result():
    r = autoloop.CycleReport(Lines())
    r.feed('{"type": "system", "subtype": "init", "mcp_servers": [{"name": "mt5", "status": "connected"}]}')
    assert any("sin resultado final" in p for p in r.problems(0, False))
    assert not any("sin resultado final" in p for p in r.problems(None, True))  # con tiempo máximo ya se dice otra cosa
    r.feed('{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "mcp__mt5__auto_status", "input": {}}]}}')
    r.feed('{"type": "result", "subtype": "error_during_execution", "is_error": true, "num_turns": 3}')
    assert [p for p in r.problems(0, False)] == ["Claude Code informó de un error en el ciclo (¿límite de turnos o de uso?)"]
    ok = autoloop.CycleReport(Lines())
    for line in ('{"type": "system", "subtype": "init", "mcp_servers": [{"name": "mt5", "status": "connected"}]}',
                 '{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "mcp__mt5__auto_status", "input": {}}]}}',
                 '{"type": "result", "subtype": "success", "is_error": false, "num_turns": 2}'):
        ok.feed(line)
    assert ok.problems(0, False) == []


def test_cycle_log_shows_what_matters_of_the_real_server_answers(tmp_path, monkeypatch):
    """La línea "←" del registro del ciclo, con respuestas que da el servidor de verdad (no escritas a mano)."""
    def line(result):
        return autoloop._tool_result_text({"type": "tool_result", "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}]})

    w = World(tmp_path, monkeypatch)
    assert w.engine.auto_status()["server_clock_known"]
    w.fake.send_none = True  # el terminal ejecuta la orden pero no contesta
    unknown = line(w.buy("EURUSD"))
    assert '"sent": false' in unknown and '"status": "unknown"' in unknown and "SIN_RESPUESTA" in unknown
    assert "la orden PUDO llegar al broker" in unknown and "summary" not in unknown
    w.fake.send_none = False
    # orden enviada con el journal sin poder escribirse después: el resultado real y el aviso, los dos a la vista
    w2 = World(tmp_path / "otro", monkeypatch, auto=AutoConfig(enabled=True, cooldown_minutes=0, one_position_per_symbol=False))
    eng = w2.new_engine(journal=FailingJournal(w2.root / "journal" / "trades.jsonl", ok_writes=1))
    sent = line(eng.auto_order("EURUSD", "buy", 1.0981, 1.1041, WHY))
    assert '"sent": true' in sent and '"status": "filled"' in sent and '"journal_error": "no se pudo anotar en el journal' in sent
    # estado con el mercado cerrado: sin hora del servidor el enfriamiento es null y el motivo sale entero
    w.fake.freeze_ticks()
    w.advance(3 * 60)
    st = w.new_engine().auto_status()
    assert st["cooldown_remaining_minutes"] is None and st["server_clock_known"] is False
    closed = line(st)
    assert '"can_open": false' in closed and "hora del servidor desconocida: no llega ninguna cotización nueva" in closed


# ---- comprobación previa -------------------------------------------------------------------------


POSITION = {"ticket": 1}
PRECHECK_CASES = [
    # nombre, cambios sobre el estado normal, ¿lanza el ciclo?, motivo exacto
    ("modo desactivado", dict(enabled=False, can_open=False, can_manage_positions=False, blockers=["el modo automático está desactivado"]),
     False, "el modo automático está desactivado en la configuración"),
    ("modo desactivado con posición", dict(enabled=False, can_open=False, can_manage_positions=False, blockers=["x"], open_auto_positions=[POSITION]),
     False, "el modo automático está desactivado en la configuración" + LEFT_OPEN_1),
    ("bloqueo de modo", dict(can_open=False, can_manage_positions=False, blockers=["kill switch activo"]),
     False, "kill switch activo"),
    ("bloqueo de modo con posición", dict(can_open=False, can_manage_positions=False, blockers=["kill switch activo", "cupo diario"], open_auto_positions=[POSITION]),
     False, "kill switch activo; cupo diario" + LEFT_OPEN_1),
    ("bloqueo de modo sin texto", dict(can_open=False, can_manage_positions=False),
     False, "el modo automático no puede actuar ahora"),
    ("reloj desconocido", dict(server_clock_known=False),
     False, autoloop.NO_QUOTES_REASON),
    ("reloj desconocido con posición", dict(server_clock_known=False, open_auto_positions=[POSITION]),
     False, autoloop.NO_QUOTES_REASON + LEFT_OPEN_NO_CLOCK),
    ("reloj desconocido que el motor explica", dict(server_clock_known=False, can_open=False, blockers=[CLOCK_BLOCKER]),
     False, CLOCK_BLOCKER),  # vale la explicación del motor: la general diría "no hay cotizaciones" cuando sí llegan
    ("reloj desconocido que el motor explica, con posición", dict(server_clock_known=False, can_open=False, blockers=[CLOCK_BLOCKER, "cupo diario"], open_auto_positions=[POSITION]),
     False, CLOCK_BLOCKER + "; cupo diario" + LEFT_OPEN_NO_CLOCK),
    ("terminal no deja operar, con posición", dict(can_open=False, terminal_can_trade=False, blockers=[ALGO_OFF], open_auto_positions=[POSITION]),
     False, ALGO_OFF + LEFT_OPEN_1),
    ("terminal no deja operar, con dos posiciones", dict(can_open=False, terminal_can_trade=False, blockers=[ALGO_OFF], open_auto_positions=[POSITION, {"ticket": 2}]),
     False, ALGO_OFF + LEFT_OPEN_2),
    ("terminal no deja operar, sin posición", dict(can_open=False, terminal_can_trade=False, blockers=[ALGO_OFF]),
     False, ALGO_OFF),
    ("bloqueo de entrada", dict(can_open=False, blockers=["enfriamiento: faltan 9 min", "cupo diario"]),
     False, "enfriamiento: faltan 9 min; cupo diario"),
    ("bloqueo de entrada con posición", dict(can_open=False, blockers=["cupo diario"], open_auto_positions=[POSITION]),
     True, "1 posición(es) del bot que revisar"),
    ("puede abrir", {},
     True, "el bot puede buscar una entrada"),
    ("puede abrir y tiene posiciones", dict(open_auto_positions=[POSITION, {"ticket": 2}]),
     True, "2 posición(es) del bot que revisar"),
]


@pytest.mark.parametrize("changes,run,reason", [c[1:] for c in PRECHECK_CASES], ids=[c[0] for c in PRECHECK_CASES])
def test_precheck_matrix_and_loop_never_launches_claude_without_work(root, monkeypatch, tmp_path, changes, run, reason):
    eng = StubEngine(status(**changes))
    assert autoloop.precheck(lambda: eng) == (run, reason) and eng.shutdowns == 1
    code, out, record = run_loop(root, monkeypatch, tmp_path, "--once", engine=StubEngine(status(**changes)))
    assert code == 0 and record.exists() is run
    assert out.has(("Hay trabajo: " if run else "Sin nada que hacer: ") + reason) and out.has("Fin del ciclo") is run


def test_precheck_reason_for_unknown_clock_does_not_assert_a_cause():
    reason = autoloop.precheck(lambda: StubEngine(status(server_clock_known=False)))[1]
    assert reason.startswith("no hay cotizaciones recientes de los símbolos de risk.toml. Puede ser: ")
    for cause in ("mercado cerrado", "EURUSD.m", "lista symbols vacía", "reloj del PC desajustado"):
        assert cause in reason
    assert "mercado sin cotizaciones recientes" not in reason  # el texto anterior daba el mercado cerrado por hecho


def test_precheck_accepts_a_status_without_terminal_can_trade_and_always_disconnects():
    old = status(open_auto_positions=[POSITION], can_open=False, blockers=["cupo diario"])
    del old["terminal_can_trade"]
    assert autoloop.precheck(lambda: StubEngine(old)) == (True, "1 posición(es) del bot que revisar")
    failing = StubEngine(error=BrokerError("sin terminal"))
    with pytest.raises(BrokerError):
        autoloop.precheck(lambda: failing)
    assert failing.shutdowns == 1


def test_precheck_with_the_real_engine(tmp_path, monkeypatch):
    """El mismo recorrido con el motor de verdad sobre el doble de MetaTrader 5 (un Engine nuevo por ciclo, como el bucle)."""
    t = [datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc).timestamp()]  # miércoles a mediodía UTC
    fake = FakeMT5(server_offset=2 * 3600)
    fake.clock = lambda: t[0]
    cfg = RiskConfig(symbols=("EURUSD",), max_open_positions=3, max_volume_per_order=1.0, max_total_volume=3.0, auto=AutoConfig(enabled=True))

    def factory():
        # sleep=fake.sleep: la espera del motor a que se mueva un tick adelanta el reloj del doble, sin esperar de verdad
        eng = Engine(MT5Broker(module=fake), cfg=cfg, root=tmp_path, journal=Journal(tmp_path / "journal" / "trades.jsonl"), sleep=fake.sleep)
        monkeypatch.setattr(eng, "_now", lambda: datetime.fromtimestamp(fake.now(), tz=timezone.utc))
        return eng

    st = factory().auto_status()
    assert {"enabled", "can_manage_positions", "server_clock_known", "terminal_can_trade", "open_auto_positions", "blockers"} <= set(st)
    assert autoloop.precheck(factory) == (True, "el bot puede buscar una entrada")
    assert factory().auto_order("EURUSD", "buy", 1.0981, 1.1041, "tendencia alcista en H1 y retroceso a la EMA20 en M15")["sent"]
    t[0] += 900
    assert autoloop.precheck(factory) == (True, "1 posición(es) del bot que revisar")

    fake.set_terminal(trade_allowed=False)  # el usuario apaga el botón Algo Trading con la posición abierta
    run, reason = autoloop.precheck(factory)
    assert run is False and "AutoTrading está desactivado" in reason and "Queda 1 posición del bot abierta sin revisar" in reason
    fake.set_terminal(trade_allowed=True)
    assert autoloop.precheck(factory)[0] is True

    fake.freeze_ticks(age=5)  # el mercado cierra: pasa el tiempo y no llega ninguna cotización
    for minutes in (15, 30, 60, 47 * 60):  # incluye antigüedades múltiplo de 30 min, que una sola lectura confundiría con otro huso
        t[0] += minutes * 60
        assert factory().auto_status()["server_clock_known"] is False
        run, reason = autoloop.precheck(factory)
        assert run is False and "Queda 1 posición del bot abierta sin revisar mientras no se conozca la hora del servidor" in reason
    fake.touch_ticks()  # el mercado reabre
    t[0] += 900
    assert autoloop.precheck(factory) == (True, "1 posición(es) del bot que revisar")


# ---- bucle ---------------------------------------------------------------------------------------


def sim_clock(sleeps=None, on_sleep=None):
    """Reloj simulado para el bucle: solo avanza cuando el bucle duerme. Devuelve (now, sleep)."""
    clock = [T0]
    calls = [0]

    def sleep(seconds):
        calls[0] += 1
        assert seconds > 0 and calls[0] < 20_000, "el bucle duerme sin avanzar"
        clock[0] += seconds
        if sleeps is not None:
            sleeps.append(seconds)
        if on_sleep:
            on_sleep(calls[0])  # número de espera, para que la prueba haga algo "mientras el bucle duerme"

    return (lambda: clock[0]), sleep


def run_loop(root, monkeypatch, tmp_path, *extra, mode="ok", engine=None, sleeps=None, on_sleep=None, **loop_kw):
    record = tmp_path / "launched.json"
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    out = Lines()
    eng = engine if engine is not None else StubEngine(status())
    now, sleep = sim_clock(sleeps, on_sleep)
    loop_kw.setdefault("engine_factory", lambda: eng)
    loop_kw.setdefault("claude", FAKE)
    code = autoloop.loop(autoloop.parse_args(list(extra)), root, out, sleep=sleep, now=now, **loop_kw)
    return code, out, record


def test_loop_refuses_when_auto_mode_is_off(root, monkeypatch, tmp_path):
    (root / "risk.toml").write_text('symbols = ["EURUSD"]\n', encoding="utf-8")
    code, out, record = run_loop(root, monkeypatch, tmp_path, "--once")
    assert code == 2 and out.has("DESACTIVADO") and out.has("[auto]") and out.has("enabled = true") and not record.exists()


def test_loop_runs_one_cycle(root, monkeypatch, tmp_path):
    code, out, record = run_loop(root, monkeypatch, tmp_path, "--once")
    assert code == 0 and record.exists() and out.has("Hay trabajo") and out.has("Fin del ciclo: 4 turnos")
    launched = json.loads(record.read_text(encoding="utf-8"))
    assert "Compra si H1 sube." in launched["prompt"] and "--permission-mode" in launched["argv"]
    assert json.loads((root / "journal" / "auto-mcp.json").read_text(encoding="utf-8"))["mcpServers"]["mt5"]["command"] == sys.executable


def test_loop_skips_cycle_when_nothing_to_do(root, monkeypatch, tmp_path):
    eng = StubEngine(status(can_open=False, blockers=["ya se abrieron 4 operaciones automáticas hoy (máximo 4)"]))
    code, out, record = run_loop(root, monkeypatch, tmp_path, "--once", engine=eng)
    assert code == 0 and not record.exists() and out.has("Sin nada que hacer: ya se abrieron 4")


def test_loop_stops_on_halt_file(root, monkeypatch, tmp_path):
    (root / "HALT").write_text("parada\n", encoding="utf-8")
    code, out, record = run_loop(root, monkeypatch, tmp_path, "--max-cycles", "5")
    assert code == 0 and not record.exists() and not out.has("──── Ciclo")
    assert out[-1].startswith("Existe el archivo HALT: el bucle termina")  # "HALT" a secas también sale en el aviso de arranque


def test_loop_stops_after_repeated_failures(root, monkeypatch, tmp_path):
    sleeps = []
    code, out, record = run_loop(root, monkeypatch, tmp_path, "--max-cycles", "10", mode="mcp_failed", sleeps=sleeps)
    assert code == 1 and out.has("3 ciclos seguidos con problemas") and out.times("──── Ciclo") == 3
    assert out.times("Próximo ciclo a las") == 2 and sum(sleeps) == pytest.approx(FIRST_WAIT + 900)
    other = tmp_path / "otro"
    other.mkdir()  # la carpeta existe: si el bucle lanzara Claude tras fallar la lectura, el registro aparecería
    broken = StubEngine(error=BrokerError("MetaTrader 5 cerrado"))
    code2, out2, record2 = run_loop(root, monkeypatch, other, "--max-cycles", "10", engine=broken)
    assert code2 == 1 and out2.times("No se pudo leer MetaTrader 5: MetaTrader 5 cerrado") == 3 and broken.calls == 3
    assert not record2.exists() and not out2.has("Fin del ciclo") and out2.has("3 ciclos seguidos con problemas")


def test_loop_failure_count_resets_after_a_good_cycle(root, monkeypatch, tmp_path):
    err = BrokerError("MetaTrader 5 cerrado")
    eng = StubEngine(outcomes=[err, err, status(can_open=False, blockers=["cupo diario"]), err, err, status(can_open=False, blockers=["cupo diario"])])
    code, out, record = run_loop(root, monkeypatch, tmp_path, "--max-cycles", "6", engine=eng)
    assert code == 0 and out.times("──── Ciclo") == 6 and not out.has("ciclos seguidos con problemas") and not record.exists()


@pytest.mark.parametrize("extra,mode,outcomes,expected", [
    (["--once"], "ok", [status()], 0),
    (["--once"], "ok", [status(can_open=False, blockers=["cupo diario"])], 0),  # ciclo saltado: no es un fallo
    (["--once"], "mcp_failed", [status()], 1),
    (["--once"], "max_turns", [status()], 1),
    (["--once"], "ok", [BrokerError("MetaTrader 5 cerrado")], 1),
    (["--max-cycles", "2"], "ok", [BrokerError("cerrado"), BrokerError("cerrado")], 1),
    (["--max-cycles", "2"], "ok", [status(), BrokerError("cerrado")], 1),
    (["--max-cycles", "2"], "ok", [BrokerError("cerrado"), status()], 0),  # se recuperó: cuenta el último ciclo
])
def test_exit_code_tells_whether_the_last_cycle_failed(root, monkeypatch, tmp_path, extra, mode, outcomes, expected):
    code, out, record = run_loop(root, monkeypatch, tmp_path, *extra, mode=mode, engine=StubEngine(outcomes=outcomes))
    assert code == expected


def test_exit_code_is_1_when_claude_cannot_be_launched(root, monkeypatch, tmp_path):
    code, out, record = run_loop(root, monkeypatch, tmp_path, "--once", "--no-precheck", claude=["no-existe-este-programa-xyz"])
    assert code == 1 and out.has("PROBLEMA: no se pudo lanzar claude")


def test_loop_waits_for_candle_close_between_cycles(root, monkeypatch, tmp_path):
    sleeps = []
    code, out, record = run_loop(root, monkeypatch, tmp_path, "--max-cycles", "3", "--interval", "15", sleeps=sleeps)
    assert code == 0 and out.times("──── Ciclo") == 3
    assert sum(sleeps) == pytest.approx(FIRST_WAIT + 900)  # hasta el cierre de la vela más el margen, y luego una vela entera
    assert all(0 < s <= autoloop.HALT_POLL_SECONDS for s in sleeps)  # a trozos cortos, para ver HALT a tiempo
    announced = [line for line in out if "Próximo ciclo a las" in line]
    assert announced == [f"Próximo ciclo a las {datetime.fromtimestamp(T0 + wait).strftime('%H:%M:%S')}." for wait in (FIRST_WAIT, FIRST_WAIT + 900)]


def test_loop_notices_halt_while_waiting(root, monkeypatch, tmp_path):
    sleeps = []

    def press_stop(n):  # el usuario pulsa "Detener el bot" en el panel durante la espera
        if n == 3:
            (root / "HALT").write_text("parada desde el panel\n", encoding="utf-8")

    code, out, record = run_loop(root, monkeypatch, tmp_path, "--interval", "60", sleeps=sleeps, on_sleep=press_stop)
    assert code == 0 and len(sleeps) == 3 and sum(sleeps) <= 3 * autoloop.HALT_POLL_SECONDS  # no espera los 53 minutos que faltaban
    assert out[-1].startswith("Existe el archivo HALT: el bucle termina") and out.times("──── Ciclo") == 1


def test_loop_does_not_announce_next_cycle_when_halt_appears_mid_cycle(root, monkeypatch, tmp_path):
    class HaltingEngine(StubEngine):
        def auto_status(self):
            (root / "HALT").write_text("parada desde el panel\n", encoding="utf-8")
            return super().auto_status()

    sleeps = []
    code, out, record = run_loop(root, monkeypatch, tmp_path, "--interval", "60", engine=HaltingEngine(status()), sleeps=sleeps)
    assert code == 0 and sleeps == [] and not out.has("Próximo ciclo") and out.times("──── Ciclo") == 1
    assert out[-1].startswith("Existe el archivo HALT: el bucle termina")


def test_loop_refuses_an_empty_symbols_list(root, monkeypatch, tmp_path, fake):
    # con la lista vacía el motor sí conoce la hora del servidor (la toma de Observación de Mercado), pero el bot no
    # tiene dónde buscar entradas: ese es el motivo de la negativa, y el único que da el bucle
    eng = Engine(MT5Broker(module=fake), cfg=RiskConfig(auto=AutoConfig(enabled=True)), root=tmp_path / "motor", sleep=fake.sleep)
    st = eng.auto_status()
    assert st["server_clock_known"] is True and st["can_open"] is False and any("lista `symbols` no vacía" in b for b in st["blockers"])
    for toml in ('symbols = []\n\n[auto]\nenabled = true\n', '[auto]\nenabled = true\n', 'symbols = ["", " "]\n\n[auto]\nenabled = true\n'):
        (root / "risk.toml").write_text(toml, encoding="utf-8")
        for flag in ("--once", "--dry-run"):
            code, out, record = run_loop(root, monkeypatch, tmp_path, flag)
            assert code == 2 and not record.exists() and not out.has("──── Ciclo") and not out.has("Comando de cada ciclo")
            assert out.has("symbols = [") and out.has("nombres exactos de tu broker") and out.has("EURUSD.m")
            # el motivo es que sin lista no sabe qué mirar; la hora del servidor sí se puede leer (Observación de Mercado)
            assert out.has("sin ella no sabe qué mirar.") and not out.has("hora del servidor")
    (root / "risk.toml").write_text('symbols = []\n', encoding="utf-8")  # sin modo automático manda el aviso de siempre
    code, out, record = run_loop(root, monkeypatch, tmp_path, "--once")
    assert code == 2 and out.has("DESACTIVADO") and not out.has("nombres exactos")


@pytest.mark.parametrize("bad,fragment", [
    ("risk_pct = 0,5", "line 5"),                                   # coma decimal: error de sintaxis TOML
    ("max_trades_per_day = 100", "auto.max_trades_per_day"),        # valor fuera de rango
    ("max_trades_per_day = [4]", "[auto] max_trades_per_day debe ser un número entero"),  # tipo equivocado
    ('enabled2 = "x"', "claves desconocidas"),
])
def test_loop_explains_an_invalid_risk_toml_at_startup(root, monkeypatch, tmp_path, bad, fragment):
    (root / "risk.toml").write_text(VALID_TOML + bad + "\n", encoding="utf-8")
    code, out, record = run_loop(root, monkeypatch, tmp_path, "--once")
    assert code == 2 and not record.exists() and not out.has("──── Ciclo")
    assert out[0].startswith("No se puede empezar; revisa risk.toml: ") and fragment in out[0]


def use_default_factory(monkeypatch, seen):
    """Deja la fábrica por defecto del bucle (relee risk.toml en cada ciclo) con un motor y un terminal de mentira."""

    class Terminal:
        def __init__(self, module=None, attach_only=False):
            seen.append(("broker", attach_only))

    class Motor(StubEngine):
        def __init__(self, broker, cfg=None, root=None, **kw):
            super().__init__(status())
            seen.append(("engine", cfg.auto.risk_pct, root))

    monkeypatch.setattr(autoloop, "MT5Broker", Terminal)
    monkeypatch.setattr(autoloop, "Engine", Motor)


@pytest.mark.parametrize("bad", ["risk_pct = 0,5", "max_trades_per_day = 100", "max_trades_per_day = [4]"])
def test_loop_survives_an_invalid_risk_toml_saved_while_running(root, monkeypatch, tmp_path, bad):
    seen = []
    use_default_factory(monkeypatch, seen)

    def edit(n):  # el usuario guarda risk.toml durante las esperas: primero mal, luego bien
        if n == 1:
            (root / "risk.toml").write_text(VALID_TOML + bad + "\n", encoding="utf-8")
        elif n == 300:
            (root / "risk.toml").write_text(VALID_TOML + "risk_pct = 0.3\n", encoding="utf-8")

    sleeps = []
    code, out, record = run_loop(root, monkeypatch, tmp_path, "--max-cycles", "3", sleeps=sleeps, on_sleep=edit, engine_factory=None)
    assert code == 0 and out.times("──── Ciclo") == 3 and out.times("Fin del ciclo") == 2
    assert out.times("No se pudo preparar el ciclo; revisa risk.toml: ") == 1 and not out.has("No se pudo leer MetaTrader 5")
    assert [x for x in seen if x[0] == "engine"] == [("engine", 0.5, root), ("engine", 0.3, root)]  # la configuración se relee en cada ciclo
    assert ("broker", True) in seen


def test_loop_stops_with_its_notice_when_risk_toml_stays_invalid(root, monkeypatch, tmp_path):
    use_default_factory(monkeypatch, [])

    def edit(n):
        if n == 1:
            (root / "risk.toml").write_text(VALID_TOML + "risk_pct = 0,5\n", encoding="utf-8")

    code, out, record = run_loop(root, monkeypatch, tmp_path, "--max-cycles", "10", sleeps=[], on_sleep=edit, engine_factory=None)
    assert code == 1 and out.times("──── Ciclo") == 4 and out.times("revisa risk.toml") == 3
    assert out[-1].startswith("3 ciclos seguidos con problemas") and out.times("Fin del ciclo") == 1


def test_loop_survives_a_missing_strategy_while_running(root, monkeypatch, tmp_path):
    def edit(n):
        if n == 1:
            (root / "strategy.md").unlink()
        elif n == 300:
            (root / "strategy.md").write_text("# Estrategia\nVende si H1 baja.\n", encoding="utf-8")

    code, out, record = run_loop(root, monkeypatch, tmp_path, "--max-cycles", "3", sleeps=[], on_sleep=edit)
    assert code == 0 and out.times("──── Ciclo") == 3 and out.times("Fin del ciclo") == 2
    assert out.times("No se pudo preparar el ciclo; revisa auto_protocol.md y strategy.md: ") == 1
    assert "Vende si H1 baja." in json.loads(record.read_text(encoding="utf-8"))["prompt"]  # la estrategia se relee en cada ciclo


def test_loop_passes_the_cycle_timeout_and_reports_denied_tools_and_problems(root, monkeypatch, tmp_path):
    seen = {}

    def spy(cmd, prompt, cwd, timeout_seconds, emit):
        seen.update(timeout=timeout_seconds, cwd=cwd, prompt=prompt)
        rep = autoloop.CycleReport(emit)
        rep.mcp_status, rep.finished, rep.tool_calls, rep.denied = "connected", True, ["auto_status"], ["mcp__mt5__order_send", "mcp__mt5__order_send"]
        return rep, ["el ciclo superó el tiempo máximo y se detuvo"]

    monkeypatch.setattr(autoloop, "run_cycle", spy)
    code, out, record = run_loop(root, monkeypatch, tmp_path, "--once", "--timeout-minutes", "3")
    assert code == 1 and seen["timeout"] == 180 and seen["cwd"] == root and "Compra si H1 sube." in seen["prompt"]
    assert out.has("Herramientas denegadas por permisos: mcp__mt5__order_send") and not out.has("order_send, mcp__mt5__order_send")
    assert out.has("PROBLEMA: el ciclo superó el tiempo máximo y se detuvo")


def test_loop_dry_run_and_missing_pieces(root, monkeypatch, tmp_path):
    code, out, record = run_loop(root, monkeypatch, tmp_path, "--dry-run")
    assert code == 0 and not record.exists() and out.has("--permission-mode dontAsk")
    (root / "strategy.md").unlink()
    code2, out2, _ = run_loop(root, monkeypatch, tmp_path, "--once")
    assert code2 == 2 and out2.has("strategy.md")


def test_console_writes_log_with_timestamps(tmp_path):
    import io

    stream = io.StringIO()
    console = autoloop.Console(tmp_path / "journal" / "auto_runs.log", stream)
    console("primera línea\nsegunda")
    log = (tmp_path / "journal" / "auto_runs.log").read_text(encoding="utf-8").splitlines()
    assert len(log) == 2 and "primera línea" in log[0] and "segunda" in stream.getvalue()


def test_interval_bounds(capsys):
    for bad in ("0", "61", "120", "240"):
        with pytest.raises(SystemExit) as exc:
            autoloop.parse_args(["--interval", bad])
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "--interval debe estar entre 1 y 60 minutos" in err and "reloj del broker, no UTC" in err
    assert autoloop.parse_args([]).interval == 15
    assert [autoloop.parse_args(["--interval", ok]).interval for ok in ("1", "30", "60")] == [1, 30, 60]
    with pytest.raises(SystemExit):
        autoloop.parse_args(["--help"])
    assert "de 1 a 60" in " ".join(capsys.readouterr().out.split())


def test_default_engine_factory_attaches_without_credentials(root, monkeypatch, tmp_path):
    created = {}

    class Spy:
        def __init__(self, module=None, attach_only=False):
            created["attach_only"] = attach_only
            raise BrokerError("sin MetaTrader en las pruebas")

    monkeypatch.setattr(autoloop, "MT5Broker", Spy)
    code, out, record = run_loop(root, monkeypatch, tmp_path, "--once", engine_factory=None)
    assert created == {"attach_only": True} and out.has("No se pudo leer MetaTrader 5: sin MetaTrader en las pruebas")
    assert code == 1 and not record.exists()  # el ciclo falló: no se lanza Claude y el código de salida lo dice
