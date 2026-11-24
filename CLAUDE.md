# Trading-A-Darle

Servidor MCP (`mt5_mcp/`) que conecta Claude Code con MetaTrader 5 en Windows, con límites de riesgo
aplicados en código (`risk.toml`) y un skill de operación (`.claude/skills/mt5-trading/SKILL.md`).

## Reglas del proyecto

- Operar solo por el servidor MCP `mt5`; nunca con scripts ad hoc ni importando `MetaTrader5` desde Bash.
- Toda orden lleva stop loss y requiere confirmación explícita del usuario para esa orden (`confirm=true` solo después).
- Única excepción, y solo si el usuario la ha activado él mismo: el modo automático. Existe cuando el usuario ha
  añadido en `risk.toml` la tabla `[auto]` con la línea `enabled = true` y ha lanzado el bucle `python -m mt5_mcp.autoloop`. Dentro de ese
  ciclo se siguen `auto_protocol.md` y `strategy.md` y se usan solo las herramientas `auto_*`, que el servidor limita
  en código. Fuera de ese ciclo, las `auto_*` no se usan nunca para evitar pedir confirmación.
- No editar `risk.toml`, `.mcp.json` ni `.claude/`, ni borrar `HALT`: son controles del usuario y están denegados.
  Activar el modo automático, permitir cuenta real o reanudar tras un `HALT` son decisiones que toma y ejecuta el usuario.
- No prometer ni proyectar ganancias. Por defecto la cuenta debe ser DEMO (`allow_live = false`).
- Pruebas: `python -m pytest -q` (corren en cualquier SO con un doble de MetaTrader5 en `tests/fake_mt5.py`).

## Estructura

- `mt5_mcp/risk.py`: motor de riesgo (lógica pura, probada).
- `mt5_mcp/broker.py`: envoltorio del paquete oficial `MetaTrader5`; `None` del terminal es error, nunca "sin datos".
- `mt5_mcp/engine.py`: lógica de cada herramienta MCP; `mt5_mcp/server.py`: registro de herramientas (SDK oficial MCP, stdio).
- `mt5_mcp/auto.py`: reglas del modo automático; `mt5_mcp/timeutil.py`: hora del servidor (MT5 no entrega UTC);
  `mt5_mcp/indicators.py`: EMA, RSI y ATR sobre velas cerradas.
- `mt5_mcp/autoloop.py`: bucle que lanza un ciclo de `claude -p` por vela; `mt5_mcp/dashboard.py`: panel en vivo local.
- `auto_protocol.md` y `strategy.md`: lo que recibe Claude en cada ciclo automático (pasos y reglas de la estrategia).
- `scripts/setup.ps1`: instalación en Windows; `scripts/check_connection.py`: diagnóstico; `scripts/kill_switch.py`: parada manual.
