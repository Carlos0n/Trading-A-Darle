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
from mt5_mcp.config import PROJECT_ROOT, RiskConfig, load_config  # noqa: E402
from mt5_mcp.engine import Engine  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--close-all", action="store_true", help="cerrar todas las posiciones abiertas a mercado")
    ap.add_argument("--reason", default="kill switch manual")
    args = ap.parse_args(argv)
    try:
        cfg = load_config()
    except Exception as exc:  # risk.toml mal escrito o ilegible: la parada no puede depender de él
        cfg = RiskConfig()
        print(f"AVISO: risk.toml no es válido ({exc}).")
        print(f"La parada sigue adelante con los valores por defecto: el archivo de parada es {cfg.halt_file}. Corrige risk.toml antes de volver a operar.")
    halt = PROJECT_ROOT / cfg.halt_file
    if not args.close_all:
        halt.parent.mkdir(parents=True, exist_ok=True)
        with halt.open("a", encoding="utf-8") as fh:
            fh.write(f"{args.reason}\n")
        print(f"HALT creado en {halt}. No se abrirán órdenes nuevas. Para reanudar: bórralo y reinicia el servidor MCP.")
        return 0
    try:
        res = Engine(MT5Broker(), cfg=cfg, root=PROJECT_ROOT).halt_trading(args.reason, close_all=True, confirm=True)
    except BrokerError as exc:
        halt.parent.mkdir(parents=True, exist_ok=True)
        with halt.open("a", encoding="utf-8") as fh:
            fh.write(f"{args.reason} (sin conexión: {exc})\n")
        print(f"HALT creado pero no se pudo conectar con MT5 para cerrar posiciones: {exc}")
        print("Cierra las posiciones a mano en el terminal.")
        return 1
    print(f"HALT creado en {res['halt_file']}")
    for c in res["closed"]:
        print(f"  ticket {c['ticket']}: {'cerrada' if c['closed'] else 'NO CERRADA DEL TODO: ' + c['retcode_name'] + ' (' + c['status'] + ')'}")
    for e in res["errors"]:
        print("  error:", e)
    if res.get("journal_error"):  # la parada y los cierres se hicieron; solo falló la anotación
        print("  aviso:", res["journal_error"])
    if res["errors"]:
        print("Revisa el terminal: alguna posición puede seguir abierta.")
    return 0 if not res["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
