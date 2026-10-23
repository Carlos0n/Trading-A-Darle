"""Kill switch manual: crea el archivo HALT y, con --close-all, cierra todas las posiciones abiertas.

Uso (Windows, con el venv):  .venv\\Scripts\\python.exe scripts\\kill_switch.py [--close-all] [--reason "texto"]
Para reanudar el trading, borra el archivo HALT a mano.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mt5_mcp.broker import BrokerError, MT5Broker  # noqa: E402
from mt5_mcp.config import PROJECT_ROOT, load_config  # noqa: E402
from mt5_mcp.engine import Engine  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--close-all", action="store_true", help="cerrar todas las posiciones abiertas")
    ap.add_argument("--reason", default="kill switch manual")
    args = ap.parse_args()
    cfg = load_config()
    halt = PROJECT_ROOT / cfg.halt_file
    if not args.close_all:
        halt.parent.mkdir(parents=True, exist_ok=True)
        with halt.open("a", encoding="utf-8") as fh:
            fh.write(f"{args.reason}\n")
        print(f"HALT creado en {halt}. No se abrirán órdenes nuevas hasta que lo borres.")
        return 0
    try:
        res = Engine(MT5Broker(), cfg=cfg).halt_trading(args.reason, close_all=True)
    except BrokerError as exc:
        with halt.open("a", encoding="utf-8") as fh:
            fh.write(f"{args.reason} (sin conexión: {exc})\n")
        print(f"HALT creado pero no se pudo conectar con MT5 para cerrar posiciones: {exc}")
        print("Cierra las posiciones a mano en el terminal.")
        return 1
    print(f"HALT creado en {res['halt_file']}")
    for c in res["closed"]:
        print(f"  ticket {c['ticket']}: {'cerrada' if c['closed'] else 'FALLO ' + c['retcode_name']}")
    for e in res["errors"]:
        print("  error:", e)
    return 0 if not res["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
