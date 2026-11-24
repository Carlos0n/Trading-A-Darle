"""Bucle del modo automático: comando, lectura de eventos, comprobación previa y control del bucle."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from mt5_mcp import autoloop, server
from mt5_mcp.broker import BrokerError

REPO = Path(__file__).resolve().parents[1]
FAKE = [sys.executable, str(REPO / "tests" / "fake_claude.py")]


@pytest.fixture
def root(tmp_path):
    (tmp_path / "risk.toml").write_text('symbols = ["EURUSD"]\n\n[auto]\nenabled = true\n', encoding="utf-8")
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


class StubEngine:
    def __init__(self, status=None, error=None):
        self.status, self.error, self.shutdowns = status, error, 0
        self.b = self

    def shutdown(self):
        self.shutdowns += 1

    def auto_status(self):
        if self.error:
            raise self.error
        return self.status


def status(**changes):
    base = {"enabled": True, "can_open": True, "can_manage_positions": True, "blockers": [], "open_auto_positions": [], "server_clock_known": True}
    base.update(changes)
    return base


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
    assert "No garantiza" in prompt or "no\n> garantiza" in prompt or "garantiza ganancias" in prompt


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
    report, problems = autoloop.run_cycle(autoloop.claude_command(FAKE, root / "m.json", None, 40), "x", root, 2, Lines())
    assert any("tiempo máximo" in p for p in problems) and not report.finished


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


# ---- comprobación previa -------------------------------------------------------------------------


def test_precheck_decides_without_spending_claude_usage():
    eng = StubEngine(status())
    assert autoloop.precheck(lambda: eng) == (True, "el bot puede buscar una entrada") and eng.shutdowns == 1
    assert autoloop.precheck(lambda: StubEngine(status(enabled=False)))[0] is False
    blocked = autoloop.precheck(lambda: StubEngine(status(can_open=False, blockers=["enfriamiento: faltan 9 min"])))
    assert blocked == (False, "enfriamiento: faltan 9 min")
    managing = autoloop.precheck(lambda: StubEngine(status(can_open=False, blockers=["cupo diario"], open_auto_positions=[{"ticket": 1}])))
    assert managing[0] is True and "1 posición" in managing[1]
    halted = autoloop.precheck(lambda: StubEngine(status(can_open=False, can_manage_positions=False, blockers=["kill switch activo"], open_auto_positions=[{"ticket": 1}])))
    assert halted[0] is False
    assert autoloop.precheck(lambda: StubEngine(status(server_clock_known=False)))[0] is False
    failing = StubEngine(error=BrokerError("sin terminal"))
    with pytest.raises(BrokerError):
        autoloop.precheck(lambda: failing)
    assert failing.shutdowns == 1


# ---- bucle ---------------------------------------------------------------------------------------


def run_loop(root, monkeypatch, tmp_path, *extra, mode="ok", engine=None, sleeps=None):
    record = tmp_path / "launched.json"
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    out = Lines()
    eng = engine if engine is not None else StubEngine(status())
    code = autoloop.loop(autoloop.parse_args(list(extra)), root, out, sleep=(sleeps.append if sleeps is not None else lambda s: None),
                         engine_factory=lambda: eng, claude=FAKE)
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
    assert code == 0 and not record.exists() and out.has("HALT")


def test_loop_stops_after_repeated_failures(root, monkeypatch, tmp_path):
    sleeps = []
    code, out, record = run_loop(root, monkeypatch, tmp_path, "--max-cycles", "10", mode="mcp_failed", sleeps=sleeps)
    assert code == 1 and out.has("3 ciclos seguidos con problemas") and len(sleeps) == 2
    broken = StubEngine(error=BrokerError("MetaTrader 5 cerrado"))
    code2, out2, record2 = run_loop(root, monkeypatch, tmp_path / "otro", "--max-cycles", "10", engine=broken)
    assert code2 == 1 and out2.has("No se pudo leer MetaTrader 5") and not record2.exists()


def test_loop_waits_for_candle_close_between_cycles(root, monkeypatch, tmp_path):
    sleeps = []
    code, out, record = run_loop(root, monkeypatch, tmp_path, "--max-cycles", "3", "--interval", "15", sleeps=sleeps)
    assert code == 0 and len(sleeps) == 2 and all(0 <= s <= 900 + 20 for s in sleeps) and out.has("Próximo ciclo a las")


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


def test_interval_bounds():
    with pytest.raises(SystemExit):
        autoloop.parse_args(["--interval", "0"])
    assert autoloop.parse_args([]).interval == 15
