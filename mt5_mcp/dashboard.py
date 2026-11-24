"""Panel en vivo: una página local que muestra cuenta, riesgo, posiciones y lo que hace el bot.

Uso (Windows, desde la raíz del repo):  .venv\\Scripts\\python.exe -m mt5_mcp.dashboard
Abre http://127.0.0.1:8787 en el navegador. Solo escucha en este PC (127.0.0.1) y solo lee; la única acción
que ofrece es el botón de parada, que crea el archivo HALT (no cierra posiciones).
En cada lectura vuelve a leer risk.toml y mira si el archivo HALT existe: no hay que reiniciarlo tras cambiarlos.

Con --demo muestra datos simulados, sin MetaTrader 5, para ver cómo es el panel.
"""

from __future__ import annotations

import argparse
import json
import random
import secrets
import sys
import tempfile
import threading
import time
import webbrowser
from collections import deque
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .broker import BrokerError, MT5Broker
from .config import PROJECT_ROOT, AutoConfig, RiskConfig, load_config
from .engine import Engine
from .journal import Journal

PAGE = Path(__file__).with_name("dashboard.html")
CACHE_SECONDS = 2.0  # el panel no debe competir con el servidor MCP por el terminal
MAX_POINTS = 2880  # 4 horas a un punto cada 5 s
CLOCK_GAP_SECONDS = 60.0  # dos lecturas más separadas que esto (en la hora del PC) no sirven para confirmar la hora del servidor


class ConfigError(Exception):
    """risk.toml no se pudo leer o no es válido."""


class Monitor:
    """Lee el estado del engine con un candado (la API de MT5 no es reentrante) y guarda la curva de equity.

    La parada no usa ese candado: solo crea el archivo HALT y lo anota en el journal, sin tocar MT5, y no puede
    quedarse esperando detrás de una lectura lenta del terminal.

    Dos relojes: la edad de la caché se mide con uno monótono, que no salta cuando cambia la hora del PC (al
    sincronizar la de Windows puede retroceder minutos y la página se quedaría con la misma foto). La separación entre
    lecturas, para la hora del servidor, se mide con la hora del PC (`wall`), que es con la que el motor las empareja.
    """

    def __init__(self, engine: Engine, demo: bool = False, on_poll: Any = None,
                 monotonic: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time):
        self.engine = engine
        self.demo = demo
        self.on_poll = on_poll
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.series: deque[list[float]] = deque(maxlen=MAX_POINTS)
        self._monotonic = monotonic
        self._wall = wall
        self._cached: dict | None = None
        self._cached_at = 0.0  # en el reloj monótono
        self._halts = 0  # paradas pedidas desde el panel
        self._read_at: float | None = None  # última lectura correcta del terminal, en la hora del PC

    def state(self) -> dict:
        with self.lock:
            cached = self._cached
            if cached is not None and self._monotonic() - self._cached_at < CACHE_SECONDS:
                return cached
            halts = self._halts
            try:
                if self.on_poll:
                    self.on_poll()
                ov = self.engine.overview(journal_entries=60)
                now = self._wall()
                equity = float(ov["account"]["equity"])
                if not self.series or now - self.series[-1][0] >= 5 or self.series[-1][1] != equity:
                    self.series.append([round(now, 1), equity])
                # La hora del servidor se confirma viendo moverse una cotización entre dos lecturas cercanas. La primera
                # lectura, la primera tras una pausa larga y la primera tras un retroceso de la hora del PC (el motor no
                # empareja una lectura con otra que, para el PC, es posterior) aún no pueden saberla: la página la
                # presenta como "comprobando" y vuelve a preguntar enseguida, en vez de dar ese bloqueo por bueno.
                known = bool(ov["auto"].get("server_clock_known", True))
                gap = None if self._read_at is None else now - self._read_at
                settling = not known and (gap is None or not 0 <= gap <= CLOCK_GAP_SECONDS)
                self._read_at = now
                out = {"ok": True, "demo": self.demo, "clock_settling": settling, "equity_series": list(self.series), **ov}
            except ConfigError as exc:
                out = self._failure(str(exc), config_error=True)
            except Exception as exc:  # el panel nunca debe caerse: muestra el error y reintenta en el siguiente sondeo
                out = self._failure(f"{type(exc).__name__}: {exc}")
            # La caché cuenta desde que TERMINA la lectura: contada desde antes, una lectura lenta nacería caducada y
            # cada sondeo repetiría la espera. Una foto que empezó antes de una parada no se guarda.
            if halts == self._halts:
                self._cached, self._cached_at = out, self._monotonic()
            return out

    def _failure(self, error: str, **extra: Any) -> dict:
        try:
            halted = self.engine.halted()  # el archivo HALT se puede mirar aunque MT5 no responda
        except OSError:
            halted = False
        return {"ok": False, "demo": self.demo, "halted": halted, "error": error, "equity_series": list(self.series), **extra}

    def halt(self) -> dict:
        res = self.engine.halt_trading("parada desde el panel en vivo")
        self._halts += 1
        self._cached = None
        return {"ok": True, "halted": res["halted"], "resume": res["resume"]}


def make_handler(monitor: Monitor):
    class Handler(BaseHTTPRequestHandler):
        server_version = "mt5-panel"

        def log_message(self, fmt: str, *args: Any) -> None:  # silencio: el panel sondea cada pocos segundos
            return

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: dict) -> None:
            self._send(code, json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            # Rechaza peticiones que lleguen con otro nombre de host (DNS rebinding desde una web externa)
            port = self.server.server_address[1]
            return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _token_ok(self) -> bool:
            # Se compara en bytes: compare_digest no admite texto con caracteres no ASCII
            given = str(self.headers.get("X-Panel-Token", ""))
            return secrets.compare_digest(given.encode("utf-8", "replace"), monitor.token.encode("ascii"))

        def do_GET(self) -> None:  # noqa: N802 - API de http.server
            if not self._host_ok():
                self._json(403, {"ok": False, "error": "host no permitido"})
            elif self.path in ("/", "/index.html"):
                html = PAGE.read_text(encoding="utf-8").replace("__PANEL_TOKEN__", monitor.token)
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self._json(200, monitor.state())
            else:
                self._json(404, {"ok": False, "error": "no existe"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._host_ok():
                self._json(403, {"ok": False, "error": "host no permitido"})
            elif self.path != "/api/halt":
                self._json(404, {"ok": False, "error": "no existe"})
            elif not self._token_ok():
                self._json(403, {"ok": False, "error": "token inválido: recarga la página"})
            else:
                try:
                    self._json(200, monitor.halt())
                except Exception as exc:
                    self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    return Handler


class PanelServer(ThreadingHTTPServer):
    # En Windows, SO_REUSEADDR deja abrir un puerto que ya usa otro panel: los dos dirían estar en la misma dirección y
    # el navegador hablaría con uno cualquiera. Sin esa opción el segundo arranque falla y main() lo avisa.
    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True


def serve(monitor: Monitor, port: int) -> ThreadingHTTPServer:
    return PanelServer(("127.0.0.1", port), make_handler(monitor))


# ---- modo real (terminal de MetaTrader 5 abierto en este PC) -----------------------------------------


def config_reloader(engine: Engine, path: Path | None = None) -> Any:
    """Gancho de sondeo que relee risk.toml, para que el panel muestre los límites con los que opera el bot.

    El bucle automático lee el archivo en cada ciclo; el panel queda abierto horas y debe ver los mismos cambios.
    Si el archivo no es válido se conserva la última configuración buena y la lectura falla con el motivo.
    """

    def on_poll() -> None:
        try:
            cfg = load_config(path)
        except Exception as exc:
            raise ConfigError(f"risk.toml no es válido ({type(exc).__name__}: {exc}). Corrígelo y el panel se recupera solo.") from exc
        if cfg.journal_file != engine.cfg.journal_file:
            engine.journal = Journal(engine.root / cfg.journal_file)
        engine.cfg = cfg

    return on_poll


def _real_engine() -> tuple[Engine, Any]:
    # El panel no envía órdenes. latch_halt=False: muestra si el archivo HALT existe ahora, sin quedarse enclavado.
    # clock_wait=0: no espera a que se mueva una cotización para fijar la hora del servidor; la página sondea cada
    # pocos segundos y la hora queda confirmada en la segunda lectura, sin bloquear la primera carga.
    engine = Engine(MT5Broker(attach_only=True), cfg=load_config(), latch_halt=False, clock_wait=0.0)
    return engine, config_reloader(engine)


# ---- modo demostración (datos simulados, sin MetaTrader 5) ------------------------------------------


def _demo_engine() -> tuple[Engine, Any]:
    tests = PROJECT_ROOT / "tests"
    if not (tests / "fake_mt5.py").exists():
        raise SystemExit("El modo --demo necesita tests/fake_mt5.py (ejecútalo desde el repositorio clonado).")
    sys.path.insert(0, str(tests))
    from fake_mt5 import FakeMT5  # noqa: PLC0415 - solo en modo demostración

    fake = FakeMT5(balance=10_000.0, equity=10_042.5, server_offset=3 * 3600)
    fake.add_symbol("GBPUSD")
    fake.set_tick("GBPUSD", 1.30120, 1.30131)
    fake.add_symbol("XAUUSD", digits=2, point=0.01, tick_size=0.01, tick_value=1.0, filling_mode=2, spread=25)
    fake.set_tick("XAUUSD", 2648.10, 2648.35)
    cfg = RiskConfig(symbols=("EURUSD", "GBPUSD", "XAUUSD"), max_volume_per_order=1.0, max_total_volume=3.0,
                     auto=AutoConfig(enabled=True, cooldown_minutes=15))
    root = Path(tempfile.mkdtemp(prefix="mt5-panel-demo-"))
    # sleep=fake.sleep: la espera a que se mueva una cotización (hora del servidor) adelanta el reloj del doble
    eng = Engine(MT5Broker(module=fake), cfg=cfg, root=root, journal=Journal(root / "journal" / "trades.jsonl"),
                 latch_halt=False, sleep=fake.sleep)
    why = "H1 alcista (EMA20 > EMA50) y retroceso a la EMA20 en M15 con RSI 44"
    eng.auto_order("EURUSD", "buy", 1.0981, 1.1041, why)
    eng.journal.record("auto_rejected", summary={"symbol": "GBPUSD", "side": "sell"}, reasons=["enfriamiento: faltan 9 min desde la última entrada (mínimo 15 min)"], reason="H1 bajista y rechazo en la EMA20")
    manual = fake.add_position("XAUUSD", "sell", 0.05, 2651.40, sl=2659.00, tp=2636.00, profit=16.5, magic=cfg.magic)
    eng.journal.record("order_sent", status="filled", summary={"symbol": "XAUUSD", "side": "sell", "volume": 0.05, "price": 2651.40, "sl": 2659.0, "tp": 2636.0, "risk_amount": 38.0, "currency": "USD"})
    fake.add_deal(profit=-48.0, commission=-1.2, entry=1, position_id=901, magic=cfg.auto_magic)
    fake.add_deal(profit=0.0, entry=0, position_id=901, magic=cfg.auto_magic, time=fake.server_now() - 5400)
    fake.add_deal(profit=71.0, commission=-1.2, entry=1, position_id=902, magic=cfg.auto_magic)
    fake.add_deal(profit=0.0, entry=0, position_id=902, magic=cfg.auto_magic, time=fake.server_now() - 3600)
    bot = fake._positions[0].ticket
    rng = random.Random(7)
    drift = {"bot": 26.0, "manual": 16.5}

    def on_poll() -> None:
        drift["bot"] += rng.uniform(-3.0, 3.4)
        drift["manual"] += rng.uniform(-1.5, 1.6)
        fake._positions = [
            p._replace(profit=round(drift["bot"], 2)) if p.ticket == bot else (p._replace(profit=round(drift["manual"], 2)) if p.ticket == manual else p)
            for p in fake._positions
        ]
        fake.set_account(equity=round(10_000.0 + drift["bot"] + drift["manual"], 2))
        fake.touch_ticks()

    return eng, on_poll


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Panel en vivo de la cuenta de MetaTrader 5 y del bot")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--demo", action="store_true", help="datos simulados, sin MetaTrader 5")
    ap.add_argument("--no-open", action="store_true", help="no abrir el navegador automáticamente")
    args = ap.parse_args(argv)

    try:
        engine, on_poll = _demo_engine() if args.demo else _real_engine()
    except BrokerError as exc:  # falta el paquete MetaTrader5 (solo existe para Windows)
        print(f"No se puede abrir el panel: {exc}")
        print("Para verlo con datos simulados, sin MetaTrader 5, añade --demo.")
        return 1
    except (ValueError, TypeError, OSError) as exc:  # risk.toml mal escrito, con un valor fuera de rango o ilegible
        print(f"No se puede abrir el panel; revisa risk.toml: {exc}")
        print("Es un archivo que solo tú editas. Corrígelo y vuelve a ejecutar este comando.")
        return 1
    monitor = Monitor(engine, demo=args.demo, on_poll=on_poll)
    try:
        httpd = serve(monitor, args.port)
    except OSError as exc:
        print(f"No se pudo abrir el puerto {args.port}: {exc}. Prueba con --port 8788.")
        return 1
    url = f"http://127.0.0.1:{args.port}"
    print(f"Panel en vivo en {url}  (Ctrl+C para cerrarlo; el bot no depende de esta ventana)")
    if args.demo:
        print("MODO DEMOSTRACIÓN: los datos son simulados.")
    if not args.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        try:
            engine.b.shutdown()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
