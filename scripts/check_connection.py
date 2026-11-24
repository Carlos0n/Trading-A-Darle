"""Comprueba la conexión con MetaTrader 5 y muestra cuenta, modo, bloqueos y límites. No envía órdenes."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mt5_mcp.broker import BrokerError, MT5Broker  # noqa: E402
from mt5_mcp.engine import Engine  # noqa: E402

CLOCK_BLOCKER = "hora del servidor desconocida"  # así empieza el bloqueo del servidor cuando no conoce la hora del broker


def main() -> int:
    print("Leyendo MetaTrader 5 (con el mercado cerrado tarda unos segundos más)...")
    try:
        eng = Engine(MT5Broker())
    except BrokerError as exc:
        print(f"ERROR: {exc}")
        return 1
    except (ValueError, TypeError, OSError) as exc:  # risk.toml mal escrito, con un valor fuera de rango o ilegible
        print(f"ERROR: revisa risk.toml: {exc}")
        return 1
    try:
        info = eng.account_info()
        status = eng.risk_status()
    except BrokerError as exc:
        print(f"ERROR: {exc}")
        return 1
    on = lambda flag: "SI" if flag else "NO"  # noqa: E731
    print("Cuenta            :", info["login"], "@", info["server"])
    print("Modo              :", info["mode"], "(demo)" if info["is_demo"] else "(REAL: dinero real)")
    print("Balance           :", info["balance"], info["currency"], "| equity:", info["equity"])
    print("Algo Trading      :", "ACTIVADO" if info["algo_trading_enabled"] else "DESACTIVADO (pulsa el botón 'Algo Trading' en MT5)")
    print(
        "API Python en MT5 :",
        "BLOQUEADA (desmarca 'Deshabilitar el trading automático a través de la API externa de Python' en Herramientas > Opciones > Asesores Expertos)"
        if info["python_api_trading_disabled"]
        else "permitida",
    )
    print("Cuenta opera      :", on(info["account_trade_allowed"]), "| permite EAs/API:", on(info["account_expert_allowed"]))
    print("Kill switch       :", "ACTIVO (archivo HALT presente o visto en esta sesión)" if info["halted"] else "inactivo")
    print("allow_live        :", info["live_trading_allowed_by_config"])
    clock_known = status["server_clock_known"]
    if clock_known:
        print("Hora del servidor : conocida (UTC%+g h)" % status["server_utc_offset_hours"])
    else:
        # El motivo va dentro del bloqueo: "hora del servidor desconocida: <motivo>. Sin ella ..."
        why = next((b for b in status["blockers"] if b.startswith(CLOCK_BLOCKER)), CLOCK_BLOCKER)
        cause = why.removeprefix(CLOCK_BLOCKER).lstrip(": ").split(". Sin ella")[0]
        print("Hora del servidor : DESCONOCIDA" + (f": {cause}" if cause else ""))
        print("   Mientras no se conozca no se abre ninguna orden; cerrar posiciones y mover stops sí se puede.")
        print("   Si el mercado está cerrado es lo esperado: repite la comprobación cuando abra.")
    print("Puede abrir       :", on(status["can_open_new_positions"]))
    for b in status["blockers"]:
        print("   bloqueo:", b)
    print("Límites           :", status["limits"])
    symbols = status["limits"]["symbols"]
    for sym in symbols if isinstance(symbols, list) else []:
        try:
            q = eng.quote(sym)
            if q["age_seconds"] is None:
                stale = " (antigüedad desconocida: falta la hora del servidor)"
            else:
                stale = f" (SIN COTIZACIÓN RECIENTE: la última es de hace {q['age_seconds']} s; mercado cerrado?)" if q["stale"] else ""
            print(f"  {sym}: bid {q['bid']} ask {q['ask']} spread {q['spread_points']} pts{stale}")
        except BrokerError as exc:
            print(f"  {sym}: no disponible ({exc})")
    if not info["is_demo"] and not info["live_trading_allowed_by_config"]:
        print("\nAVISO: cuenta REAL con allow_live=false: el servidor rechazará abrir posiciones (correcto hasta que decidas lo contrario).")
    eng.b.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
