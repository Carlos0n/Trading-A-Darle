"""Bucle del modo automático: lanza un ciclo de Claude Code, sin interacción, al cierre de cada vela.

Uso (Windows, desde la raíz del repo):  .venv\\Scripts\\python.exe -m mt5_mcp.autoloop
Opciones: --interval 15 (minutos, de 1 a 60), --once (un solo ciclo), --model <alias>, --no-precheck, --dry-run.

Qué hace en cada ciclo:
1. Si existe el archivo HALT, termina. También lo mira cada pocos segundos mientras espera al siguiente ciclo.
2. Consulta MetaTrader 5 directamente (sin gastar uso de Claude). Salta el ciclo si Claude no podría hacer nada:
   el bot está bloqueado, no hay cotizaciones recientes, o no puede abrir y no tiene posiciones que el terminal
   le deje tocar.
3. Lanza `claude -p` con SOLO las herramientas del modo automático y las de lectura. Las órdenes manuales
   (order_send, close_position, modify_position, halt_trading) quedan fuera: siguen exigiendo tu confirmación.
4. Muestra en vivo lo que hace Claude y lo guarda en journal/auto_runs.log.

Los límites no dependen de este script ni del texto que escriba el modelo: los aplica el servidor MCP.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TextIO

from .broker import BrokerError, MT5Broker
from .config import PROJECT_ROOT, load_config
from .engine import Engine

SERVER = "mt5"
# Herramientas que el ciclo automático puede usar. Todo lo demás se deniega (modo dontAsk).
ALLOWED_TOOLS = (
    "auto_status", "auto_order", "auto_close", "auto_tighten_stop",
    "bars", "quote", "symbol_info", "positions", "history_today", "journal_tail", "account_info", "risk_status",
)
# Herramientas que se ocultan al modelo durante el ciclo: las que exigen confirmación humana y las que no necesita.
HIDDEN_TOOLS = ("order_send", "close_position", "modify_position", "halt_trading", "order_check", "position_size", "pending_orders")

PROTOCOL_PATH = Path("auto_protocol.md")
STRATEGY_PATH = Path("strategy.md")
LOG_PATH = Path("journal") / "auto_runs.log"
MCP_CONFIG_PATH = Path("journal") / "auto-mcp.json"
MAX_CONSECUTIVE_FAILURES = 3
HALT_POLL_SECONDS = 2.0  # durante la espera entre ciclos, el archivo HALT se mira como mucho cada 2 s
# Motivo para saltar un ciclo cuando no se conoce la hora del servidor y el motor no explica por qué.
# No afirma una causa: hay varias posibles.
NO_QUOTES_REASON = (
    "no hay cotizaciones recientes de los símbolos de risk.toml. Puede ser: mercado cerrado, nombres que tu broker "
    "escribe distinto (por ejemplo EURUSD.m), lista symbols vacía o reloj del PC desajustado"
)


def mcp_name(tool: str) -> str:
    return f"mcp__{SERVER}__{tool}"


def build_prompt(root: Path) -> str:
    """Instrucciones del ciclo: el protocolo (auto_protocol.md) más la estrategia del usuario, en un solo mensaje."""
    protocol = root / PROTOCOL_PATH
    strategy = root / STRATEGY_PATH
    if not protocol.exists():
        raise FileNotFoundError(f"falta {protocol}: describe los pasos de cada ciclo")
    if not strategy.exists():
        raise FileNotFoundError(f"falta {strategy}: es el archivo con las reglas que el bot debe seguir")
    return (
        "Ejecuta ahora UN ciclo del modo automático y termina. No hay nadie para responder preguntas.\n\n"
        "=== PROTOCOLO DEL CICLO (auto_protocol.md) ===\n"
        f"{protocol.read_text(encoding='utf-8').strip()}\n\n"
        "=== ESTRATEGIA (strategy.md) ===\n"
        f"{strategy.read_text(encoding='utf-8').strip()}\n"
    )


def write_mcp_config(root: Path, python_exe: str) -> Path:
    """Configuración MCP propia del bucle: el servidor mt5 con la ruta absoluta del Python que ejecuta este script."""
    env = {"MT5_MCP_ROOT": str(root), "PYTHONUTF8": "1"}
    if os.environ.get("MT5_TERMINAL_PATH"):
        env["MT5_TERMINAL_PATH"] = os.environ["MT5_TERMINAL_PATH"]
    cfg = {"mcpServers": {SERVER: {"type": "stdio", "command": python_exe, "args": ["-m", "mt5_mcp"], "env": env}}}
    path = root / MCP_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return path


def claude_command(claude: list[str], mcp_config: Path, model: str | None, max_turns: int) -> list[str]:
    """Línea de comandos de un ciclo. El mensaje va por la entrada estándar, no como argumento."""
    cmd = [
        *claude,
        "-p",
        "--mcp-config", str(mcp_config), "--strict-mcp-config",  # solo el servidor mt5, y espera a que conecte
        "--permission-mode", "dontAsk",  # lo que no esté permitido explícitamente se deniega sin preguntar
        "--tools=",  # sin herramientas integradas (ni Bash, ni edición de archivos)
        "--allowedTools", *[mcp_name(t) for t in ALLOWED_TOOLS],
        "--disallowedTools", *[mcp_name(t) for t in HIDDEN_TOOLS],
        "--max-turns", str(max_turns),
        "--output-format", "stream-json", "--verbose",
    ]
    if model:
        cmd += ["--model", model]
    return cmd


def next_boundary(now_ts: float, interval_minutes: int, offset_seconds: int) -> float:
    """Próximo cierre de vela (múltiplo del intervalo desde la hora en punto) más unos segundos de margen."""
    step = interval_minutes * 60
    nxt = (int(now_ts) // step + 1) * step + offset_seconds
    if nxt - now_ts > step:  # aún no ha pasado el margen del cierre actual
        nxt -= step
    return float(nxt)


# ---- lectura en vivo de los eventos de Claude Code (--output-format stream-json) ---------------------


def _short(value: Any, limit: int = 220) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tool_result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, list):
        content = " ".join(str(c.get("text", "")) if isinstance(c, dict) else str(c) for c in content)
    text = "" if content is None else str(content)
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return _short(text)
    if isinstance(data, dict):
        keys = ("error", "journal_error", "sent", "closed", "modified", "status", "retcode_name", "reasons", "enabled", "can_open", "blockers", "trades_today")
        picked = {k: data[k] for k in keys if k in data}
        if picked:
            return _short(picked, 400)
    return _short(data)


class CycleReport:
    """Convierte los eventos de un ciclo en líneas legibles y recoge lo necesario para juzgar si funcionó."""

    def __init__(self, emit: Callable[[str], None]):
        self.emit = emit
        self.mcp_status: str | None = None
        self.denied: list[str] = []
        self.tool_calls: list[str] = []
        self.finished = False
        self.is_error = False
        self.turns = 0
        self.cost: float | None = None
        self.final_text = ""

    def feed(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            e = json.loads(line)
        except ValueError:
            self.emit(_short(line, 400))
            return
        if not isinstance(e, dict):
            return
        kind, sub = e.get("type"), e.get("subtype")
        if kind == "system" and sub == "init":
            for srv in e.get("mcp_servers") or []:
                if isinstance(srv, dict) and srv.get("name") == SERVER:
                    self.mcp_status = str(srv.get("status"))
            self.emit(f"Servidor {SERVER}: {self.mcp_status or 'no aparece'}")
        elif kind == "system" and sub == "permission_denied":
            name = str(e.get("tool_name") or e.get("tool") or "?")
            self.denied.append(name)
            self.emit(f"DENEGADO por permisos: {name}")
        elif kind == "assistant":
            for block in (e.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and str(block.get("text", "")).strip():
                    self.emit("Claude: " + str(block["text"]).strip())
                elif block.get("type") == "tool_use":
                    name = str(block.get("name", "")).replace(f"mcp__{SERVER}__", "")
                    self.tool_calls.append(name)
                    self.emit(f"  → {name} {_short(block.get('input') or {}, 300)}")
        elif kind == "user":
            content = (e.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self.emit("  ← " + _tool_result_text(block))
        elif kind == "result":
            self.finished = True
            self.is_error = bool(e.get("is_error")) or (sub not in (None, "success"))
            self.turns = int(e.get("num_turns") or 0)
            cost = e.get("total_cost_usd")
            self.cost = float(cost) if isinstance(cost, (int, float)) else None
            self.final_text = str(e.get("result") or "")
            for d in e.get("permission_denials") or []:
                name = str(d.get("tool_name", "?")) if isinstance(d, dict) else str(d)
                if name not in self.denied:
                    self.denied.append(name)

    def problems(self, exit_code: int | None, timed_out: bool) -> list[str]:
        out: list[str] = []
        if timed_out:
            out.append("el ciclo superó el tiempo máximo y se detuvo")
        if self.mcp_status != "connected":
            out.append(f"el servidor {SERVER} no conectó (estado: {self.mcp_status or 'desconocido'}); ¿MetaTrader 5 abierto?")
        if exit_code not in (0, None) and not timed_out:
            out.append(f"claude terminó con código {exit_code}")
        if self.is_error:
            out.append("Claude Code informó de un error en el ciclo (¿límite de turnos o de uso?)")
        if not self.finished and not timed_out:
            out.append("el ciclo terminó sin resultado final")
        if self.mcp_status == "connected" and self.finished and not self.is_error and "auto_status" not in self.tool_calls:
            out.append("Claude no consultó auto_status: el texto del ciclo no es fiable, mira el journal")
        return out


def _kill_tree(proc: subprocess.Popen) -> None:
    """Mata el proceso y sus hijos (en Windows, el servidor MCP cuelga de él)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
        else:
            proc.kill()
    except OSError:
        pass


def run_cycle(cmd: list[str], prompt: str, cwd: Path, timeout_seconds: int, emit: Callable[[str], None]) -> tuple[CycleReport, list[str]]:
    """Ejecuta un ciclo y devuelve el informe y la lista de problemas (vacía si todo fue normal)."""
    report = CycleReport(emit)
    env = dict(os.environ, PYTHONUTF8="1")
    try:
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, encoding="utf-8", errors="replace", bufsize=1)
    except OSError as exc:
        return report, [f"no se pudo lanzar claude: {exc}"]
    timed_out = threading.Event()

    def watchdog() -> None:
        timed_out.set()
        _kill_tree(proc)

    timer = threading.Timer(timeout_seconds, watchdog)
    timer.daemon = True
    timer.start()
    stderr_lines: list[str] = []

    def drain_stderr() -> None:
        for line in proc.stderr or []:
            if line.strip():
                stderr_lines.append(line.strip())

    err_thread = threading.Thread(target=drain_stderr, daemon=True)
    err_thread.start()
    try:
        if proc.stdin:
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
            except OSError:
                pass
        for line in proc.stdout or []:
            report.feed(line)
        code = proc.wait()
    finally:
        timer.cancel()
        err_thread.join(timeout=2)
    for line in stderr_lines[-5:]:
        emit("claude (aviso): " + _short(line, 300))
    return report, report.problems(code, timed_out.is_set())


# ---- comprobación previa (sin gastar uso de Claude) ---------------------------------------------------


def _unattended(count: int, why: str = "") -> str:
    """Aviso que acompaña a un ciclo saltado cuando el bot tiene posiciones abiertas."""
    if not count:
        return ""
    if count == 1:
        return f". Queda 1 posición del bot abierta sin revisar{why}; conserva el SL y el TP que tiene en el broker"
    return f". Quedan {count} posiciones del bot abiertas sin revisar{why}; conservan el SL y el TP que tienen en el broker"


def precheck(engine_factory: Callable[[], Engine]) -> tuple[bool, str]:
    """¿Hay algo que hacer en este ciclo? Lee MetaTrader 5 directamente.

    Solo hay trabajo si Claude podría hacer algo: revisar posiciones del bot que el terminal deja tocar, o buscar
    una entrada sin bloqueos. Con la hora del servidor desconocida el ciclo se salta entero: el bot no puede abrir
    y, si es porque el mercado está cerrado, el broker tampoco deja cerrar ni mover un stop.
    """
    engine = engine_factory()
    try:
        st = engine.auto_status()
    finally:
        try:
            engine.b.shutdown()
        except Exception:
            pass
    open_count = len(st["open_auto_positions"])
    blockers = "; ".join(st["blockers"])
    if not st["enabled"]:
        return False, "el modo automático está desactivado en la configuración" + _unattended(open_count)
    if not st["can_manage_positions"]:  # HALT o cuenta no permitida: las herramientas auto_* no harían nada
        return False, (blockers or "el modo automático no puede actuar ahora") + _unattended(open_count)
    if not st["server_clock_known"]:
        # Si el motor explica por qué no conoce la hora del servidor, vale su explicación; si no, la general.
        return False, (blockers or NO_QUOTES_REASON) + _unattended(open_count, " mientras no se conozca la hora del servidor")
    if open_count and st.get("terminal_can_trade", True):
        return True, f"{open_count} posición(es) del bot que revisar"
    if blockers:  # no puede abrir; y si tiene posiciones, el terminal no deja cerrarlas ni mover su stop
        return False, blockers + _unattended(open_count)
    return True, "el bot puede buscar una entrada"


# ---- bucle ------------------------------------------------------------------------------------------


class Console:
    """Escribe con hora en la consola y en journal/auto_runs.log."""

    def __init__(self, log_path: Path, stream: TextIO | None = None):
        self.log_path = log_path
        self.stream = stream or sys.stdout
        log_path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        for part in text.splitlines() or [""]:
            line = f"[{stamp}] {part}"
            try:
                print(line, file=self.stream, flush=True)
            except UnicodeEncodeError:
                print(line.encode("ascii", "replace").decode("ascii"), file=self.stream, flush=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"{datetime.now().strftime('%Y-%m-%d')} {line}\n")


def find_claude(explicit: str | None) -> list[str] | None:
    if explicit:
        return [explicit]
    found = shutil.which("claude")
    return [found] if found else None


def loop(args: argparse.Namespace, root: Path, say: Callable[[str], None], sleep: Callable[[float], None] = time.sleep,
         engine_factory: Callable[[], Engine] | None = None, claude: list[str] | None = None,
         now: Callable[[], float] = time.time) -> int:
    """Bucle principal. Devuelve 0 si terminó bien, 1 si el último ciclo tuvo problemas (o hubo 3 seguidos) y 2 si no pudo empezar."""
    try:
        cfg = load_config(root / "risk.toml")
    except (ValueError, TypeError, OSError) as exc:  # sintaxis, tipo equivocado, valor fuera de rango o archivo ilegible
        say(f"No se puede empezar; revisa risk.toml: {exc}")
        say("Es un archivo que solo tú editas. Corrígelo y vuelve a ejecutar este comando.")
        return 2
    if not cfg.auto.enabled:
        say("El modo automático está DESACTIVADO. Para activarlo, abre risk.toml y añade al final estas dos líneas:")
        say("    [auto]")
        say("    enabled = true")
        say("Es un interruptor que solo tú debes tocar. Después vuelve a ejecutar este comando.")
        return 2
    if not cfg.symbols:
        say("El modo automático necesita una lista de símbolos: sin ella no sabe qué mirar ni puede leer la hora del servidor.")
        say("Abre risk.toml y escribe symbols = [...] con los nombres exactos de tu broker, por ejemplo:")
        say('    symbols = ["EURUSD", "XAUUSD"]')
        say("Si tu broker los escribe con sufijo (EURUSD.m), ponlos así. Es un archivo que solo tú editas. Después vuelve a ejecutar este comando.")
        return 2
    claude = claude or find_claude(args.claude)
    if not claude:
        say("No encuentro el comando 'claude'. Instala Claude Code o indica la ruta con --claude.")
        return 2
    try:
        build_prompt(root)
    except FileNotFoundError as exc:
        say(f"No se puede empezar: {exc}")
        return 2
    except (OSError, ValueError) as exc:  # no se puede abrir, o no está guardado como texto UTF-8
        say(f"No se puede empezar; revisa auto_protocol.md y strategy.md: {exc}")
        return 2
    mcp_config = write_mcp_config(root, sys.executable)
    cmd = claude_command(claude, mcp_config, args.model, args.max_turns)
    factory = engine_factory or (lambda: Engine(MT5Broker(attach_only=True), cfg=load_config(root / "risk.toml"), root=root))
    halt = root / cfg.halt_file
    a = cfg.auto
    say(f"Modo automático: un ciclo cada {args.interval} min. Riesgo {cfg.auto_risk_pct} % por operación, máximo {a.max_trades_per_day} "
        f"operaciones al día, se detiene tras {a.max_consecutive_losses} pérdidas seguidas. Cuenta real: {'permitida' if cfg.allow_live and a.allow_live else 'NO permitida (solo demo)'}.")
    say("Para detenerlo: Ctrl+C en esta ventana, o crea el archivo HALT (botón del panel en vivo).")
    if args.dry_run:
        say("Prueba en seco. Comando de cada ciclo:")
        say("  " + " ".join(cmd))
        return 0

    cycles = failures = 0
    first = True
    try:
        while True:
            if not first and not halt.exists():
                wake = next_boundary(now(), args.interval, args.offset_seconds)
                say(f"Próximo ciclo a las {datetime.fromtimestamp(wake).strftime('%H:%M:%S')}.")
                # Espera a trozos cortos para ver el archivo HALT en cuanto aparece, no al final del intervalo.
                while not halt.exists() and (left := wake - now()) > 0:
                    sleep(min(left, HALT_POLL_SECONDS))
            first = False
            if halt.exists():
                say("Existe el archivo HALT: el bucle termina. Para reanudar, bórralo tú y vuelve a lanzar este comando.")
                return 0
            cycles += 1
            say(f"──── Ciclo {cycles} ────")
            run, problem = True, False
            if not args.no_precheck:
                try:
                    run, why = precheck(factory)
                    say(("Hay trabajo: " if run else "Sin nada que hacer: ") + why)
                except (BrokerError, OSError) as exc:
                    run, problem = False, True
                    say(f"No se pudo leer MetaTrader 5: {exc}")
                except (ValueError, TypeError) as exc:  # risk.toml mal escrito o con un valor fuera de rango
                    run, problem = False, True
                    say(f"No se pudo preparar el ciclo; revisa risk.toml: {exc}")
            prompt = ""
            if run:
                try:
                    prompt = build_prompt(root)
                except (OSError, ValueError) as exc:  # auto_protocol.md o strategy.md borrados o ilegibles con el bucle en marcha
                    run, problem = False, True
                    say(f"No se pudo preparar el ciclo; revisa auto_protocol.md y strategy.md: {exc}")
            if run:
                report, problems = run_cycle(cmd, prompt, root, args.timeout_minutes * 60, say)
                cost = f", coste estimado {report.cost:.2f} USD" if report.cost is not None else ""
                say(f"Fin del ciclo: {report.turns} turnos, {len(report.tool_calls)} llamadas a herramientas{cost}.")
                if report.denied:
                    say("Herramientas denegadas por permisos: " + ", ".join(sorted(set(report.denied))))
                for item in problems:
                    say("PROBLEMA: " + item)
                problem = bool(problems)
            failures = failures + 1 if problem else 0
            if failures >= MAX_CONSECUTIVE_FAILURES:
                say(f"{failures} ciclos seguidos con problemas: el bucle se detiene para no gastar uso. Revisa MetaTrader 5, Claude Code y risk.toml.")
                return 1
            if args.once or (args.max_cycles and cycles >= args.max_cycles):
                return 1 if failures else 0  # un programa que lance --once debe ver que el ciclo falló
    except KeyboardInterrupt:
        say("Detenido por el usuario (Ctrl+C). Las posiciones abiertas conservan su SL y TP en el broker.")
        return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Bucle del modo automático de trading con Claude Code")
    ap.add_argument("--interval", type=int, default=15, help="minutos entre ciclos, de 1 a 60, alineados al cierre de vela (por defecto 15)")
    ap.add_argument("--offset-seconds", type=int, default=20, help="segundos de margen tras el cierre de la vela")
    ap.add_argument("--once", action="store_true", help="ejecutar un solo ciclo y salir")
    ap.add_argument("--max-cycles", type=int, default=0, help="parar tras N ciclos (0 = sin límite)")
    ap.add_argument("--model", default=None, help="modelo de Claude para los ciclos (por defecto, el de tu Claude Code)")
    ap.add_argument("--max-turns", type=int, default=40, help="tope de pasos de Claude por ciclo")
    ap.add_argument("--timeout-minutes", type=int, default=8, help="tiempo máximo de un ciclo")
    ap.add_argument("--claude", default=None, help="ruta al ejecutable de Claude Code si no está en el PATH")
    ap.add_argument("--no-precheck", action="store_true", help="no consultar MT5 antes de cada ciclo")
    ap.add_argument("--dry-run", action="store_true", help="mostrar el comando de cada ciclo sin ejecutar nada")
    args = ap.parse_args(argv)
    if not 1 <= args.interval <= 60:
        ap.error("--interval debe estar entre 1 y 60 minutos: las velas de más de 1 h siguen el reloj del broker, no UTC, "
                 "y el ciclo llegaría horas después de su cierre. Para una estrategia H4 o D1 usa 60")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = PROJECT_ROOT
    return loop(args, root, Console(root / LOG_PATH))


if __name__ == "__main__":
    sys.exit(main())
