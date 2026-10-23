---
name: mt5-trading
description: Operar en MetaTrader 5 a través del servidor MCP "mt5" con límites de riesgo. Usar cuando el usuario pida ver su cuenta, cotizaciones, posiciones, calcular lotes, abrir, modificar o cerrar operaciones, o detener el trading. Toda orden exige stop loss y confirmación explícita del usuario para esa orden concreta.
---

# Operar en MetaTrader 5 (servidor MCP `mt5`)

El servidor `mt5` aplica los límites de `risk.toml` en código: tú propones, él decide. Tu trabajo es
seguir este flujo, mostrar números claros y no enviar nada sin confirmación explícita del usuario.

## Reglas que no se negocian

- Nunca llames a `order_send`, `close_position`, `modify_position` ni `halt_trading(close_all=true)` con
  `confirm=true` sin que el usuario haya confirmado **esa acción concreta** en su propio mensaje. Una confirmación
  vale para una sola acción con los parámetros mostrados; "haz lo que creas", "ok" o una confirmación anticipada
  no sirven: vuelve a preguntar.
- Sin stop loss no hay orden. Nunca subas el volumen, acerques el SL ni abras otra posición "para recuperar".
- Si una orden falla, se ejecuta parcialmente o el motor la rechaza: informa y pregunta. No reintentes en bucle ni
  completes el resto por tu cuenta.
- No edites `risk.toml`, `.mcp.json` ni `.claude/settings.json`; no borres `HALT`. Son controles del usuario.
- No prometas resultados ni proyectes ganancias. El objetivo operativo es respetar los límites.

## Antes de cualquier operación

1. Llama a `account_info` y `risk_status`.
2. Di en una línea: cuenta, modo (DEMO o REAL), equity, P&L de hoy y margen que queda antes del límite diario.
3. Si `risk_status.blockers` no está vacío (kill switch, AutoTrading apagado, API de Python bloqueada, cuenta REAL
   sin `allow_live`, pérdida diaria, horario, posiciones máximas): explica el motivo y **no intentes abrir**.
   Que `can_open_new_positions` sea true no garantiza que una orden pase: el veredicto por orden lo da `order_check`.
4. Si el modo es REAL, recuérdalo en cada resumen de orden.

## Abrir una posición

1. `quote(symbol)` y, si hace falta, `symbol_info(symbol)`. Mira `stale` y `age_seconds`: si la cotización tiene
   más de unos minutos (o es fin de semana), el mercado está probablemente cerrado y el precio es el último
   conocido: dilo y no pidas confirmación de ninguna orden hasta que haya cotización nueva.
2. El usuario debe dar el **stop loss** (precio). Si no lo da, pídelo. El servidor no expone velas ni indicadores:
   no inventes un SL "por estructura o ATR"; como mucho indica la distancia mínima del broker (`stops_level`) y el
   spread, y espera su respuesta.
3. `order_check(symbol, side, sl, tp)`: evalúa riesgo, límites y el `order_check` del broker sin enviar nada.
4. Muestra el resumen en una tabla a partir de `summary`: símbolo, lado, precio, **volumen**, SL, TP, puntos al SL,
   riesgo en divisa y en % del equity, spread, avisos.
5. Pregunta literalmente: **"¿Confirmas enviar esta orden? Responde CONFIRMO."**
6. Solo si el usuario confirma en su propio mensaje, llama a `order_send(..., confirm=true)` pasando **`volume`
   con el valor exacto del resumen** (además de símbolo, lado, SL, TP y `comment`). Si el servidor responde que el
   volumen supera el máximo permitido, o el `fill_price` difiere del resumen en más del spread, el precio se movió:
   vuelve al paso 3 y pide confirmación otra vez.
7. Informa `status`, `retcode_name`, ticket (`order`), `fill_price` y `fill_volume`. Si `status` es `partial` o
   `placed`, di qué volumen se ejecutó frente al pedido y **no envíes otra orden por el resto** sin repetir los
   pasos 4-6. Llama a `positions` para verificar.

## Modificar o cerrar

- `modify_position(ticket, sl, tp, confirm=true)` solo tras confirmación. Nunca se quita el SL (`tp=0` sí quita el TP).
- `close_position(ticket, confirm=true)` solo tras confirmación. Cerrar y modificar siempre están permitidos,
  incluso con `HALT` o con cuenta REAL sin `allow_live`. Si devuelve `closed=false` con `status=partial`, queda
  volumen abierto: muéstralo con `positions` y pregunta.
- Si `modify_position` o `close_position` devuelven `no existe una posición abierta con ticket N` o
  `POSITION_CLOSED`, lo más probable es que el SL/TP ya la cerrara: compruébalo con `history_today`, informa el
  P&L realizado y no abras otra posición para "reponerla".
- Para parar sin cerrar nada: `halt_trading(reason)` en cuanto el usuario pida detener (no necesita confirmación).
  `close_all=true` cierra TODAS las posiciones a mercado: úsalo solo si el usuario pide explícitamente cerrarlas;
  antes muestra `positions` y pregunta literalmente **"¿Cierro estas N posiciones a mercado? Responde CONFIRMO."**
  y pasa `confirm=true`. Avisa que reanudar exige borrar `HALT` a mano y reiniciar el servidor (`/mcp`).

## Registro

Pon el motivo de la decisión en el `comment` de la orden (máximo 31 caracteres) y resume la operación en el chat.
El journal técnico lo escribe el servidor en `journal/trades.jsonl`; consúltalo con `journal_tail`.

## Diagnóstico rápido

| Síntoma | Causa probable | Qué decir |
|---|---|---|
| `initialize() falló (-10005...)` | MT5 cerrado o sin sesión; varias instalaciones | Abrir MT5 con la cuenta; definir `MT5_TERMINAL_PATH` |
| `AutoTrading está desactivado` | Botón "Algo Trading" apagado | El usuario lo pulsa en la barra de MT5 |
| `API externa de Python` en blockers, o `order_send` con `TRADE_DISABLED` / `CLIENT_DISABLES_AT` | Casilla de MT5 "Deshabilitar el trading automático a través de la API externa de Python" marcada | El usuario la desmarca en Herramientas > Opciones > Asesores Expertos |
| `cuenta ... es REAL y allow_live=false` | Protección por defecto | Solo el usuario cambia `allow_live` en `risk.toml` y reinicia el servidor |
| `trading detenido (kill switch)` | `HALT` presente o visto en esta sesión | El usuario borra `HALT` y reinicia el servidor cuando decida reanudar |
| `símbolo X no existe en este broker` | Nombre distinto (`EURUSD.m`, `EURUSDm`, `EURUSD.pro`); el servidor sugiere parecidos | Pedir el nombre exacto de Observación de Mercado; **el usuario** lo añade a `symbols` en `risk.toml` y reinicia |
| `MARKET_CLOSED`, `PRICE_OFF`, `REQUOTE` | Mercado cerrado o precio movido | Informar, no reintentar; esperar cotización nueva y repetir el flujo |
| `no existe una posición abierta` / `POSITION_CLOSED` | La cerró el SL/TP o un cierre manual | `history_today`, informar el P&L y no reponerla |
