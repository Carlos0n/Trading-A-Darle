# Trading-A-Darle: Claude Code + MetaTrader 5 (Windows local)

Servidor MCP propio que permite a Claude Code leer tu cuenta de MetaTrader 5, calcular tamaño de posición
y enviar, modificar o cerrar órdenes **con límites de riesgo aplicados en código**. Construido solo con
piezas oficiales: el paquete `MetaTrader5` de MetaQuotes, el SDK oficial de MCP y el mecanismo de skills,
permisos y `.mcp.json` de Claude Code.

> No existe ninguna herramienta que garantice una ganancia diaria. Este proyecto no promete rentabilidad:
> su objetivo es que cada operación respete un riesgo máximo, un tope de pérdida diaria y un kill switch.
> Empieza y permanece en cuenta DEMO hasta tener semanas de registro en el journal.

## Qué hace

| Herramienta MCP | Qué hace | Permiso en Claude Code |
|---|---|---|
| `account_info`, `risk_status` | Cuenta, modo DEMO/REAL, P&L de hoy, límites y bloqueos globales | automático |
| `quote`, `symbol_info`, `positions`, `pending_orders`, `history_today`, `journal_tail` | Lecturas | automático |
| `position_size`, `order_check` | Calculan volumen por riesgo y evalúan la orden sin enviarla | automático |
| `order_send`, `modify_position`, `close_position` | Escriben en el broker; exigen `confirm=true` | pregunta cada vez |
| `halt_trading` | Kill switch: crea `HALT`; con `close_all=true` y `confirm=true` cierra todo | pregunta cada vez |

Límites (en `risk.toml`, los aplica el servidor, no el modelo): solo DEMO salvo `allow_live = true`,
riesgo máximo por operación en % del equity (volumen calculado a partir del SL), pérdida diaria máxima
(realizada más flotante, sin contar depósitos del día), posiciones y lotes máximos, lista blanca de símbolos,
horario, spread máximo y SL obligatorio. Si el terminal falla al responder (posiciones, histórico), el servidor
rechaza la orden en vez de suponer que no hay nada abierto.

## Requisitos

- Windows 10/11 x64 con MetaTrader 5 instalado y una cuenta **demo** abierta en él.
- Python 3.11 a 3.14 (instalador de python.org, con el lanzador `py`). Con 3.15 no hay binarios de `MetaTrader5`.
- Claude Code instalado en Windows.

## Instalación (una vez)

1. Clona este repositorio y abre PowerShell en su carpeta.
2. Ejecuta el instalador: elige un Python válido, crea `.venv`, instala `mcp`, `MetaTrader5` y `pytest`, corre
   las pruebas y guarda la variable de usuario `MT5_MCP_PYTHON` que usa `.mcp.json` para lanzar el servidor:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
   ```

3. En MetaTrader 5, abre tu cuenta demo y en **Herramientas > Opciones > Asesores Expertos**:
   - **Desmarca** "Deshabilitar el trading automático a través de la API externa de Python". Viene marcada por
     defecto y, mientras lo esté, MT5 rechaza toda orden enviada desde Python.
   - Deja encendido el botón **Algo Trading / AutoTrading** de la barra de herramientas. El servidor se niega a
     abrir posiciones cuando está apagado, así que te sirve de interruptor de emergencia inmediato.
4. Comprueba la conexión (no envía órdenes):

   ```powershell
   .venv\Scripts\python.exe scripts\check_connection.py
   ```

   Debe mostrar `Modo: DEMO`, `Algo Trading: ACTIVADO`, `API Python en MT5: permitida` y `Puede abrir: SI`.
5. **Cierra la terminal y abre una nueva** (para que vea `MT5_MCP_PYTHON`), entra en la carpeta y lanza `claude`.
   Acepta el servidor `mt5` del proyecto cuando lo pregunte y verifica con `/mcp`.

Si tienes varias instalaciones de MT5, define `MT5_TERMINAL_PATH` con la ruta a `terminal64.exe`.
No hace falta guardar contraseñas: el servidor usa la sesión ya abierta en el terminal. Si quisieras que
inicie sesión por sí mismo, define `MT5_LOGIN`, `MT5_PASSWORD` y `MT5_SERVER` como variables de entorno
de usuario (nunca en archivos del repo).

## Uso

Dentro de Claude Code, el skill `mt5-trading` se activa solo. Ejemplos:

- "¿Cómo está mi cuenta y cuánto margen me queda hoy?"
- "Cotización de EURUSD y tamaño de posición para comprar con SL en 1.0820"
- "Compra EURUSD con SL 1.0820 y TP 1.0900" → Claude muestra la tabla y pide **CONFIRMO** antes de enviar.
- "Cierra el ticket 12345" → pide confirmación y cierra.
- "Detén todo" → `halt_trading`; "y cierra las posiciones" → pide confirmación y usa `close_all=true`.

Cada orden exige dos barreras: Claude pide tu confirmación en el chat, y Claude Code te pide permiso para
la herramienta de escritura (`ask` en `.claude/settings.json`). El modo `bypassPermissions` está deshabilitado.
Los nombres de símbolo se usan tal como los llama tu broker (`EURUSD.m`, `XAUUSD.raw`); si escribes uno en
otra combinación de mayúsculas, el servidor lo resuelve contra la lista del terminal.

## Kill switch y reanudación

- Desde Claude: `halt_trading`. Desde PowerShell: `.venv\Scripts\python.exe scripts\kill_switch.py --close-all`.
- Manual e inmediato: apaga **Algo Trading / AutoTrading** en MT5 (el servidor deja de abrir posiciones) o
  vuelve a marcar la casilla de la API de Python, o cierra el terminal. Estos son los únicos frenos que el modelo
  no puede tocar.
- Mientras exista el archivo `HALT` en la raíz, el servidor rechaza abrir posiciones, y una vez visto queda
  **enclavado**: borrar el archivo no reanuda nada hasta que reinicies el servidor MCP (`/mcp` en Claude Code o
  reiniciando Claude Code). Cerrar y modificar posiciones sigue permitido.
- Reanudar: borra `HALT` tú mismo y reinicia el servidor.

## Qué controlas tú y qué no puede tocar Claude

`.claude/settings.json` deniega a Claude editar `risk.toml`, `.mcp.json` y el propio `settings.json`, y le
impide borrar `HALT` con los comandos habituales o importar `MetaTrader5` desde Bash. Son reglas de texto:
cubren lo que nombran y sirven para que no haya atajos accidentales, no para detener a alguien decidido. El
freno real está en MT5 (botón AutoTrading y casilla de la API de Python) y en que cada escritura pasa por tu
permiso explícito. Cambia `risk.toml` a mano y reinicia el servidor después.

## Journal

Cada evaluación, orden enviada, rechazo, cierre (total o parcial), modificación y parada se escribe en
`journal/trades.jsonl` (ignorado por git porque contiene el número de cuenta). Consúltalo con `journal_tail`.

## Pasar a cuenta real

Solo cuando el journal en demo muestre semanas de operativa dentro de límites: cambia `allow_live = true`
en `risk.toml`, reinicia el servidor y vuelve a ejecutar `check_connection.py`. Empieza con lotes mínimos.
Las pérdidas en real son reales; los avisos de los brokers indican que la mayoría de cuentas minoristas
pierden dinero.

## Pruebas

```powershell
.venv\Scripts\python.exe -m pytest -q
```

Las pruebas usan un doble del paquete `MetaTrader5` (`tests/fake_mt5.py`), así que corren en cualquier
sistema y cubren el motor de riesgo, la construcción de órdenes, cierres parciales, errores del terminal,
el kill switch enclavado, el journal y la frontera MCP (`confirm=false` por defecto en toda escritura).

## Limitaciones conocidas

- El tamaño de posición usa `trade_tick_value_loss` del broker (o `trade_tick_value` si es mayor). Según MetaQuotes,
  ese valor suele venir en la divisa de la cuenta, pero algunos brokers lo expresan en la divisa base del símbolo:
  compara el riesgo que muestra `order_check` con el que calcula tu terminal antes de confiar en él.
- El "día" para el límite de pérdida diaria es el día UTC. Comisiones y swaps cuentan; cargos sueltos del
  broker (ajustes, intereses) no.
- Solo órdenes a mercado; no hay órdenes pendientes porque no aportan nada al control de riesgo inicial.
- Si Claude Code se lanza desde una terminal abierta antes de ejecutar `setup.ps1`, no verá `MT5_MCP_PYTHON`
  y el servidor no arrancará: abre una terminal nueva.

## Estructura

```
.mcp.json                      servidor "mt5" (stdio) para Claude Code
.claude/settings.json          permisos: lecturas automáticas, escrituras con pregunta, bypass deshabilitado
.claude/skills/mt5-trading/    skill con el flujo de operación y las reglas
risk.toml                      límites de riesgo (los aplica el servidor)
mt5_mcp/                       risk.py (motor), broker.py (MT5), engine.py (herramientas), server.py (MCP)
scripts/                       setup.ps1, check_connection.py, kill_switch.py
tests/                         pruebas con doble de MetaTrader5
journal/                       trades.jsonl (generado)
```
