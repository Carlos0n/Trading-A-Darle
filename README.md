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
| `bars` | Velas cerradas con EMA20, EMA50, RSI14, ATR14 y tendencia, calculados por el servidor | pregunta la primera vez |
| `position_size`, `order_check` | Calculan volumen por riesgo y evalúan la orden sin enviarla | automático |
| `order_send`, `modify_position`, `close_position` | Escriben en el broker; exigen `confirm=true` | pregunta cada vez |
| `halt_trading` | Kill switch: crea `HALT`; con `close_all=true` y `confirm=true` cierra todo | pregunta cada vez |
| `auto_status`, `auto_order`, `auto_close`, `auto_tighten_stop` | Modo automático: operan sin confirmación, solo si tú lo activaste | aprobadas de antemano solo en el bucle que tú lanzas; en una sesión normal Claude Code pregunta |

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

## Modo automático

Por defecto está apagado y cada orden pide tu confirmación. Si lo enciendes, Claude ejecuta un ciclo al cierre de
cada vela de 15 minutos: lee el estado, aplica las reglas de `strategy.md` y, si se cumplen, abre, protege o cierra
posiciones sin preguntarte. No hay garantía de ganancia: la estrategia incluida es una plantilla sin validar.
Úsalo en cuenta demo.

**Encenderlo (lo haces tú, a mano):**

1. Abre `risk.toml` y añade al final estas dos líneas:

   ```toml
   [auto]
   enabled = true
   ```

2. Comprueba que `symbols` en `risk.toml` lleva los nombres exactos de tu broker, tal como salen en Observación
   de Mercado (por ejemplo `EURUSD.m`). Con la lista vacía el bucle no arranca y te dice qué escribir. Con nombres
   que tu broker no usa, el bot no abre nada y el motivo sale en el bucle y en el panel.
3. Revisa `strategy.md`. Es lo que el bot seguirá. Cámbialo con tus reglas si quieres.
4. Con MetaTrader 5 abierto, lanza el bucle desde la carpeta del proyecto:

   ```powershell
   .venv\Scripts\python.exe -m mt5_mcp.autoloop
   ```

   La ventana muestra en vivo cada paso de Claude y lo guarda en `journal/auto_runs.log`. Para probar un solo
   ciclo usa `--once` (termina con código 1 si el ciclo falló); para ver el comando sin ejecutar nada, `--dry-run`.
   El intervalo se cambia con `--interval`, entre 1 y 60 minutos.

**Apagarlo:** Ctrl+C en esa ventana o el botón "Detener el bot" del panel, que crea el archivo `HALT`: el bucle
lo ve en unos segundos y termina. Si hay un ciclo en marcha, termina al acabar ese ciclo; mientras tanto el
servidor ya rechaza las herramientas `auto_*`. Apagar Algo Trading en MT5 también lo frena: el bucle sigue
abierto, pero no llama a Claude ni puede operar hasta que lo vuelvas a encender.

Si guardas `risk.toml` con un error mientras el bucle corre, el ciclo se anota como fallido con el motivo y el bucle
se recupera al corregirlo. Tras tres ciclos fallidos seguidos se detiene solo.

**Límites propios del modo automático** (además de todos los generales), ajustables en la tabla `[auto]`:

| Clave | Por defecto | Qué hace |
|---|---|---|
| `risk_pct` | 0.5 | % del equity que arriesga cada operación del bot. Nunca supera `max_risk_pct_per_trade` |
| `max_trades_per_day` | 4 | Operaciones que puede abrir en un día (UTC) |
| `max_consecutive_losses` | 2 | Tras esas pérdidas seguidas, no abre más hasta el día siguiente |
| `cooldown_minutes` | 15 | Espera mínima entre dos entradas, también de un día para otro |
| `min_rr` | 1.5 | Beneficio/riesgo mínimo. El TP es obligatorio |
| `one_position_per_symbol` | true | No abre en un símbolo que ya tiene posición |
| `max_quote_age_seconds` | 60 | No decide con cotizaciones más viejas |
| `allow_live` | false | En cuenta real el bot solo opera si esto y `allow_live` general son true |

Cómo está construido para que el control siga siendo tuyo:

- El bucle lanza cada ciclo con `claude -p` en modo `dontAsk` y una lista cerrada de herramientas: las `auto_*` y
  las de lectura. Sin Bash ni edición de archivos. Las órdenes manuales siguen exigiendo tu confirmación y en el
  bucle quedan denegadas.
- El volumen lo fija el servidor a partir del stop loss. El modelo no puede elegir el tamaño.
- El bot solo cierra o mueve stops de posiciones que abrió él, y solo puede acercar el stop, nunca alejarlo.
- Con `HALT` activo el bot no toca nada. Las posiciones conservan su SL y TP en el broker.
- Antes de cada ciclo el bucle consulta MT5 directamente. Si no hay nada que hacer (cupo agotado, enfriamiento,
  mercado sin cotizaciones, Algo Trading apagado), no llama a Claude y no gasta uso de tu plan.
- Cada orden del bot se anota en `journal/trades.jsonl` antes de enviarse, con el motivo que dio. Si no se puede
  escribir el journal, no se abre nada. Cerrar y mover stops nunca dependen del journal.
- Los interruptores de `risk.toml` se escriben sin comillas (`enabled = true`). Con comillas el servidor no arranca
  y dice cuál corregir, en vez de darlos por activados.
- Fuera del bucle no hay modo automático. En una conversación normal Claude sigue pidiéndote confirmación para cada
  orden y tiene instrucciones de no usar las `auto_*` para saltársela. Si Claude Code te pide permiso para
  `auto_order`, `auto_close` o `auto_tighten_stop` en una sesión normal, no lo concedas "para siempre".

Cada ciclo consume uso de tu plan de Claude, igual que una conversación. Empieza con el intervalo de 15 minutos.

## Verlo en vivo

- **MetaTrader 5.** Es la fuente de verdad: las posiciones del bot aparecen con el comentario `claude-auto`.
- **En el móvil, con la app oficial MetaTrader 5.** Es la forma más simple de seguirlo fuera de casa:
  1. En el PC crea una contraseña de inversor (solo lectura) en Herramientas > Opciones > Servidor > Cambiar,
     eligiendo el tipo Inversor.
  2. En la app, conecta una cuenta existente con tu número de cuenta, esa contraseña y el mismo servidor.
     Verás posiciones, órdenes e historial en tiempo real, sin poder operar desde el móvil.
  3. Para recibir un aviso en cada operación: copia el MetaQuotes ID de la app (sección Mensajes) y pégalo en el PC
     en Herramientas > Opciones > Notificaciones, activando las notificaciones push del terminal.
- **Panel en vivo.** En otra ventana de PowerShell:

  ```powershell
  .venv\Scripts\python.exe -m mt5_mcp.dashboard
  ```

  Abre `http://127.0.0.1:8787` con equity, resultado del día frente al límite, posiciones, lo que hace el bot y
  por qué, y un botón de parada. Solo se ve desde ese PC y solo lee. Con `--demo` enseña datos simulados para
  conocerlo sin MT5. El panel vuelve a leer `risk.toml` en cada actualización y mira si `HALT` existe, así que
  muestra los límites y el estado reales sin reiniciarlo. Avisa en rojo si MetaTrader 5 pierde la conexión con el
  broker, y marca los importes de hoy como "día sin verificar" cuando no conoce la hora del servidor. En Windows un
  segundo panel en el mismo puerto no arranca: usa `--port`. El panel se conecta al terminal como un segundo
  programa, sin iniciar sesión. MetaQuotes indica que varios programas pueden usar el mismo terminal, pero no
  garantiza que no se estorben: si ves errores de conexión en el bot, cierra el panel. El bot no depende de él.
- **La ventana del bucle.** Muestra cada herramienta que usa Claude y su resultado.
- **Desde el móvil con la app de Claude.** Los ciclos del bucle no aparecen en la app, porque cada uno es un
  proceso sin ventana. Lo que sí puedes hacer es dejar abierta en el PC una sesión aparte para preguntarle:

  ```powershell
  claude --remote-control "MT5"
  ```

  La sesión aparece en la pestaña Code de la app de Claude. Desde ahí pregunta, por ejemplo, "¿cómo va el bot hoy?"
  y Claude lee `auto_status`, `positions` y `journal_tail`. Claude Code te pedirá permiso la primera vez para
  `auto_status` y `bars`, que son lecturas. Esa sesión no opera sola: el bot sigue siendo el bucle, y cualquier
  orden que pidas ahí exige tu confirmación. Con Remote Control la conversación, con saldos y órdenes, se guarda en
  servidores de Anthropic.

## Kill switch y reanudación

- Desde Claude: `halt_trading`. Desde PowerShell: `.venv\Scripts\python.exe scripts\kill_switch.py --close-all`.
- Manual e inmediato: apaga **Algo Trading / AutoTrading** en MT5 (el servidor deja de abrir posiciones) o
  vuelve a marcar la casilla de la API de Python, o cierra el terminal. Estos son los únicos frenos que el modelo
  no puede tocar.
- Mientras exista el archivo `HALT` en la raíz, el servidor rechaza abrir posiciones, y una vez visto queda
  **enclavado**: borrar el archivo no reanuda nada hasta que reinicies el servidor MCP (`/mcp` en Claude Code o
  reiniciando Claude Code). Cerrar y modificar posiciones sigue permitido.
- Reanudar: borra `HALT` tú mismo y reinicia el servidor. El bucle automático termina a los pocos segundos de
  aparecer `HALT`, o al acabar el ciclo que esté en marcha; para reanudarlo, borra el archivo y vuelve a lanzar
  el bucle. El panel muestra si el archivo existe en ese momento, así que su aviso desaparece al borrarlo sin
  tener que reiniciarlo.

## Qué controlas tú y qué no puede tocar Claude

`.claude/settings.json` deniega a Claude editar `risk.toml`, `.mcp.json` y el propio `settings.json`, y le
impide borrar `HALT` con los comandos habituales o importar `MetaTrader5` desde Bash. Son reglas de texto:
cubren lo que nombran y sirven para que no haya atajos accidentales, no para detener a alguien decidido. El
freno real está en MT5 (botón AutoTrading y casilla de la API de Python) y en que cada escritura manual pasa por
tu permiso explícito. Las del modo automático solo existen si tú lo activaste en `risk.toml`. Cambia `risk.toml`
a mano y reinicia el servidor después.

## Journal

Cada evaluación, orden enviada, rechazo, cierre (total o parcial), modificación y parada se escribe en
`journal/trades.jsonl` (ignorado por git porque contiene el número de cuenta). Consúltalo con `journal_tail`.
Cada apertura deja dos líneas: el intento, antes de enviarse, y el resultado. Si el journal no se puede escribir
no se abre nada; cerrar, mover stops y la parada siguen funcionando y avisan con `journal_error`. Si el terminal
no confirma una orden, el resultado es `unknown`: no se reintenta y hay que mirar las posiciones.

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
el kill switch enclavado, el journal, la frontera MCP (`confirm=false` por defecto en toda escritura), el modo
automático de punta a punta, el bucle con un sustituto de `claude` y el panel en vivo.

## Limitaciones conocidas

- El tamaño de posición usa `trade_tick_value_loss` del broker (o `trade_tick_value` si es mayor). Según MetaQuotes,
  ese valor suele venir en la divisa de la cuenta, pero algunos brokers lo expresan en la divisa base del símbolo:
  compara el riesgo que muestra `order_check` con el que calcula tu terminal antes de confiar en él.
- El "día" para el límite de pérdida diaria es el día UTC. Cuentan operaciones, comisiones, swaps y cualquier
  cargo del broker. No cuentan tus depósitos, retiradas ni bonos.
- El bot reconoce sus posiciones por un identificador propio en las órdenes y, además, por un registro que escribe
  al abrir (`journal/auto_positions.jsonl`), porque algunos brokers borran ese identificador. Si borras ese archivo
  con posiciones del bot abiertas en un broker así, el bot dejará de reconocerlas y de contarlas.
- MetaTrader 5 entrega las horas en el reloj del servidor del broker, no en UTC, y no dice cuál es el desfase. El
  servidor MCP lo deduce solo cuando ve moverse una cotización entre dos lecturas seguidas. Consecuencias: al
  arrancar puede tardar unos segundos en confirmarlo; con el mercado cerrado la hora del servidor queda como
  desconocida, las cotizaciones se marcan como paradas, no se abre ninguna orden y el bucle no llama a Claude; y
  en un mercado con muy pocos ticks puede saltarse algún ciclo. Cerrar posiciones y mover stops a mano no dependen
  de esto, pero mientras la hora sea desconocida el bucle tampoco revisa las posiciones del bot: conservan su SL
  y su TP en el broker.
- El reloj de Windows debe estar en hora. Atrasado más de unos 30 segundos o adelantado más de unos 2 minutos, el
  servidor no puede fiarse de la hora de las cotizaciones y no abre nada, con un aviso que lo dice. El modo
  automático es más estricto: con el reloj adelantado más de un minuto ya ve las cotizaciones como viejas y no abre.
- Los ciclos se alinean al cierre de vela en UTC. Para velas de hasta una hora coincide con el reloj del broker;
  por eso `--interval` no admite más de 60 minutos.
- El modo automático no sabe de noticias ni de calendario económico: solo ve precios e indicadores.
- Solo órdenes a mercado; no hay órdenes pendientes porque no aportan nada al control de riesgo inicial.
- Si Claude Code se lanza desde una terminal abierta antes de ejecutar `setup.ps1`, no verá `MT5_MCP_PYTHON`
  y el servidor no arrancará: abre una terminal nueva.

## Estructura

```
.mcp.json                      servidor "mt5" (stdio) para Claude Code
.claude/settings.json          permisos: lecturas automáticas, escrituras con pregunta, bypass deshabilitado
.claude/skills/mt5-trading/    skill con el flujo de operación y las reglas
risk.toml                      límites de riesgo (los aplica el servidor)
auto_protocol.md, strategy.md  pasos y reglas que recibe Claude en cada ciclo automático
mt5_mcp/                       risk.py (motor), broker.py (MT5), engine.py (herramientas), server.py (MCP)
                               auto.py (reglas del bot), autoloop.py (bucle), dashboard.py (panel en vivo)
scripts/                       setup.ps1, check_connection.py, kill_switch.py
tests/                         pruebas con doble de MetaTrader5
journal/                       trades.jsonl (generado)
```
