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
| `account_info`, `risk_status` | Cuenta, modo DEMO/REAL, P&L de hoy, límites y si se puede abrir | automático |
| `quote`, `symbol_info`, `positions`, `pending_orders`, `history_today`, `journal_tail` | Lecturas | automático |
| `position_size`, `order_check` | Calculan volumen por riesgo y evalúan la orden sin enviarla | automático |
| `order_send`, `modify_position`, `close_position` | Escriben en el broker; exigen `confirm=true` | pregunta cada vez |
| `halt_trading` | Kill switch: crea `HALT` y opcionalmente cierra todo | pregunta cada vez |

Límites (en `risk.toml`, los aplica el servidor, no el modelo): solo DEMO salvo `allow_live = true`,
riesgo máximo por operación en % del equity (volumen calculado a partir del SL), pérdida diaria máxima,
posiciones y lotes máximos, lista blanca de símbolos, horario, spread máximo y SL obligatorio.

## Requisitos

- Windows 10/11 x64 con MetaTrader 5 instalado y una cuenta **demo** abierta en él.
- Python 3.11 a 3.14 (instalador de python.org, con el lanzador `py`).
- Claude Code instalado en Windows.

## Instalación (una vez)

1. Clona este repositorio y abre PowerShell en su carpeta.
2. Ejecuta el instalador: crea `.venv`, instala `mcp`, `MetaTrader5` y `pytest`, corre las pruebas y guarda
   la variable de usuario `MT5_MCP_PYTHON` que usa `.mcp.json`:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
   ```

3. Abre MetaTrader 5 con tu cuenta demo y habilita dos cosas en **Herramientas > Opciones > Asesores Expertos**:
   - **Desmarca** "Deshabilitar el trading automático a través de la API externa de Python"
     (viene marcada por defecto; mientras esté marcada, MT5 rechaza toda orden enviada desde Python).
   - Deja activo el botón **Algo Trading / AutoTrading** de la barra de herramientas. Este servidor se niega a abrir
     posiciones cuando ese botón está apagado, así que te sirve de interruptor de emergencia inmediato.
4. Comprueba la conexión (no envía órdenes):

   ```powershell
   .venv\Scripts\python.exe scripts\check_connection.py
   ```

   Debe mostrar `Modo: DEMO`, `Algo Trading: ACTIVADO` y `Puede operar: True`.
5. Abre una terminal nueva (para que vea `MT5_MCP_PYTHON`), entra en la carpeta y lanza `claude`.
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
- "Detén todo y cierra las posiciones" → `halt_trading(close_all=true)`.

Cada orden exige dos barreras: Claude pide tu confirmación en el chat, y Claude Code te pide permiso para
la herramienta de escritura (`ask` en `.claude/settings.json`). El modo `bypassPermissions` está deshabilitado.

## Kill switch y reanudación

- Desde Claude: `halt_trading`. Desde PowerShell: `.venv\Scripts\python.exe scripts\kill_switch.py --close-all`.
- Manual e inmediato: apaga **Algo Trading / AutoTrading** en MT5 (el servidor deja de abrir posiciones) o cierra el terminal.
- Mientras exista el archivo `HALT` en la raíz, el servidor rechaza abrir posiciones. Cerrar sigue permitido.
- Reanudar: borra `HALT` tú mismo. Claude tiene prohibido hacerlo (`deny` en settings y regla del skill).

## Journal

Cada evaluación, orden enviada, rechazo, cierre, modificación y parada se escribe en `journal/trades.jsonl`
(ignorado por git porque contiene el número de cuenta). Consúltalo con `journal_tail` o ábrelo como texto.

## Pasar a cuenta real

Solo cuando el journal en demo muestre semanas de operativa dentro de límites: cambia `allow_live = true`
en `risk.toml`, reinicia Claude Code y vuelve a ejecutar `check_connection.py`. Empieza con lotes mínimos.
Las pérdidas en real son reales; los avisos de los brokers indican que la mayoría de cuentas minoristas
pierden dinero.

## Pruebas

```powershell
.venv\Scripts\python.exe -m pytest -q
```

Las pruebas usan un doble del paquete `MetaTrader5` (`tests/fake_mt5.py`), así que corren en cualquier
sistema y cubren el motor de riesgo, la construcción de órdenes, el kill switch y el journal.

## Limitaciones conocidas

- El tamaño de posición usa `trade_tick_value_loss` del broker (o `trade_tick_value` si es mayor). Según MetaQuotes,
  ese valor suele venir en la divisa de la cuenta, pero algunos brokers lo expresan en la divisa base del símbolo:
  compara el riesgo que muestra `order_check` con el que calcula tu terminal antes de confiar en él.
- El "día" para el límite de pérdida diaria es el día UTC.
- Solo órdenes a mercado; no hay órdenes pendientes porque no aportan nada al control de riesgo inicial.

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
