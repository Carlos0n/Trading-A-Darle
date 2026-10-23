# Trading-A-Darle

Servidor MCP (`mt5_mcp/`) que conecta Claude Code con MetaTrader 5 en Windows, con límites de riesgo
aplicados en código (`risk.toml`) y un skill de operación (`.claude/skills/mt5-trading/SKILL.md`).

## Reglas del proyecto

- Operar solo por el servidor MCP `mt5`; nunca con scripts ad hoc ni llamando a `MetaTrader5` directamente desde Bash.
- Toda orden lleva stop loss y requiere confirmación explícita del usuario (`confirm=true` solo después).
- No editar `risk.toml` ni borrar `HALT` por iniciativa propia: son controles del usuario.
- No prometer ni proyectar ganancias. Por defecto la cuenta debe ser DEMO (`allow_live = false`).
- Pruebas: `python -m pytest -q` (corren en cualquier SO con un doble de MetaTrader5 en `tests/fake_mt5.py`).

## Estructura

- `mt5_mcp/risk.py`: motor de riesgo (lógica pura, probada).
- `mt5_mcp/broker.py`: envoltorio del paquete oficial `MetaTrader5`.
- `mt5_mcp/engine.py`: lógica de cada herramienta MCP; `mt5_mcp/server.py`: registro de herramientas (FastMCP, stdio).
- `scripts/setup.ps1`: instalación en Windows; `scripts/check_connection.py`: diagnóstico; `scripts/kill_switch.py`: parada manual.
