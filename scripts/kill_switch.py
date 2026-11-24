"""Kill switch manual: crea el archivo HALT y, con --close-all, cierra todas las posiciones abiertas.

Uso (Windows, con el venv):  .venv\\Scripts\\python.exe scripts\\kill_switch.py [--close-all] [--reason "texto"]
Para reanudar el trading: borra el archivo HALT a mano y reinicia el servidor MCP (/mcp en Claude Code).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mt5_mcp.broker import BrokerError, MT5Broker  # noqa: E402
from mt5_mcp.config import PROJECT_ROOT, RiskConfig, declared_halt_file, load_config  # noqa: E402
from mt5_mcp.engine import Engine  # noqa: E402

NOTHING_STOPPED = "No queda nada detenido: crea ese archivo a mano o apaga el botón Algo Trading en MT5."


def _append_halt(halt: Path, line: str) -> str | None:
    """Añade una línea al archivo de parada. Devuelve por qué no existe el archivo si no se pudo crear; None si existe."""
    try:
        halt.parent.mkdir(parents=True, exist_ok=True)
        with halt.open("a", encoding="utf-8") as fh:
            fh.write(f"{line}\n")
    except OSError as exc:
        if not halt.exists():  # con el archivo (o una carpeta con ese nombre) ya presente, la parada vale igual
            return f"no se pudo crear el archivo de parada {halt} ({exc})"
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--close-all", action="store_true", help="cerrar todas las posiciones abiertas a mercado")
    ap.add_argument("--reason", default="kill switch manual")
    args = ap.parse_args(argv)
    try:
        cfg = load_config()
    except Exception as exc:  # risk.toml mal escrito o ilegible: la parada no puede depender de él
        # el nombre del archivo de parada se recupera aunque falle otra clave: es el que miran el servidor y el bucle
        cfg = RiskConfig(halt_file=declared_halt_file())
        print(f"AVISO: risk.toml no es válido ({exc}).")
        print(f"La parada sigue adelante con los valores por defecto, salvo el archivo de parada, que es {cfg.halt_file}. "
              "Corrige risk.toml antes de volver a operar.")
    halt = PROJECT_ROOT / cfg.halt_file
    if not args.close_all:
        error = _append_halt(halt, args.reason)
        if error:
            print(f"ERROR: {error}. {NOTHING_STOPPED}")
            return 1
        print(f"HALT creado en {halt}. No se abrirán órdenes nuevas. Para reanudar: bórralo y reinicia el servidor MCP.")
        return 0
    try:
        res = Engine(MT5Broker(), cfg=cfg, root=PROJECT_ROOT).halt_trading(args.reason, close_all=True, confirm=True)
    except BrokerError as exc:
        error = _append_halt(halt, f"{args.reason} (sin conexión: {exc})")
        print(f"ERROR: {error}. {NOTHING_STOPPED}" if error else f"HALT creado en {halt}.")
        print(f"No se pudo conectar con MT5 para cerrar posiciones: {exc}")
        print("Cierra las posiciones a mano en el terminal.")
        return 1
    halt_ok = Path(res["halt_file"]).exists()
    if halt_ok:
        print(f"HALT creado en {res['halt_file']}")
    else:  # el motor lo dice también entre los errores, con el motivo del sistema
        print(f"ERROR: no se pudo crear el archivo de parada {res['halt_file']}. {NOTHING_STOPPED}")
    for c in res["closed"]:
        print(f"  ticket {c['ticket']}: {'cerrada' if c['closed'] else 'NO CERRADA DEL TODO: ' + c['retcode_name'] + ' (' + c['status'] + ')'}")
    for e in res["errors"]:
        print("  error:", e)
    if res.get("journal_error"):  # la parada y los cierres se hicieron; solo falló la anotación
        print("  aviso:", res["journal_error"])
    # Errores de cierre (no el del archivo de parada): una posición sin cerrar, o posiciones que no se pudieron leer
    halt_error = f"no se pudo crear el archivo {res['halt_file']} "
    if any(not e.startswith(halt_error) for e in res["errors"]):
        print("Revisa el terminal: alguna posición puede seguir abierta.")
    return 0 if halt_ok and not res["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
