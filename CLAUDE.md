# Trading-A-Darle

Servidor MCP (`mt5_mcp/`) que conecta Claude Code con MetaTrader 5 en Windows, con límites de riesgo
aplicados en código (`risk.toml`) y un skill de operación (`.claude/skills/mt5-trading/SKILL.md`).

## Reglas del proyecto

- Operar solo por el servidor MCP `mt5`; nunca con scripts ad hoc ni importando `MetaTrader5` desde Bash.
- Toda orden lleva stop loss y requiere confirmación explícita del usuario para esa orden (`confirm=true` solo después).
- No editar `risk.toml`, `.mcp.json` ni `.claude/`, ni borrar `HALT`: son controles del usuario y están denegados.
  Reanudar tras un `HALT` exige que el usuario borre el archivo y reinicie el servidor MCP (`/mcp`).
- No prometer ni proyectar ganancias. Por defecto la cuenta debe ser DEMO (`allow_live = false`).
- Pruebas: `python -m pytest -q` (corren en cualquier SO con un doble de MetaTrader5 en `tests/fake_mt5.py`).

## Estructura

- `mt5_mcp/risk.py`: motor de riesgo (lógica pura, probada).
- `mt5_mcp/broker.py`: envoltorio del paquete oficial `MetaTrader5`; `None` del terminal es error, nunca "sin datos".
- `mt5_mcp/engine.py`: lógica de cada herramienta MCP; `mt5_mcp/server.py`: registro de herramientas (SDK oficial MCP, stdio).
- `scripts/setup.ps1`: instalación en Windows; `scripts/check_connection.py`: diagnóstico; `scripts/kill_switch.py`: parada manual.
