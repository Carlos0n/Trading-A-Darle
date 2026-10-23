"""Comprueba la conexión con MetaTrader 5 y muestra cuenta, modo y límites. No envía órdenes."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mt5_mcp.broker import BrokerError, MT5Broker  # noqa: E402
from mt5_mcp.engine import Engine  # noqa: E402


def main() -> int:
    try:
        eng = Engine(MT5Broker())
        info = eng.account_info()
        status = eng.risk_status()
    except BrokerError as exc:
        print(f"ERROR: {exc}")
        return 1
    print("Cuenta       :", info["login"], "@", info["server"])
    print("Modo         :", info["mode"], "(DEMO)" if info["is_demo"] else "(REAL: dinero real)")
    print("Balance      :", info["balance"], info["currency"], "| equity:", info["equity"])
    print("Algo Trading :", "ACTIVADO" if info["algo_trading_enabled"] else "DESACTIVADO (pulsa el botón 'Algo Trading' en MT5)")
    print("Kill switch  :", "ACTIVO (archivo HALT presente)" if info["halted"] else "inactivo")
    print("allow_live   :", info["live_trading_allowed_by_config"])
    print("Puede operar :", status["can_open_new_positions"])
    print("Límites      :", status["limits"])
    for sym in status["limits"]["symbols"] if isinstance(status["limits"]["symbols"], list) else []:
        try:
            q = eng.quote(sym)
            print(f"  {sym}: bid {q['bid']} ask {q['ask']} spread {q['spread_points']} pts")
        except BrokerError as exc:
            print(f"  {sym}: no disponible ({exc})")
    print("\nRecuerda: en MT5, Herramientas > Opciones > Asesores Expertos, la casilla 'Deshabilitar el trading automático")
    print("a través de la API externa de Python' debe estar DESMARCADA; si no, el broker rechazará las órdenes desde aquí.")
    if not info["is_demo"] and not info["live_trading_allowed_by_config"]:
        print("\nAVISO: cuenta REAL con allow_live=false: el servidor rechazará toda orden (correcto hasta que decidas lo contrario).")
    eng.b.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
