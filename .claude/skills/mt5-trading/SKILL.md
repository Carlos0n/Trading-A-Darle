---
name: mt5-trading
description: Operar en MetaTrader 5 a través del servidor MCP "mt5" con límites de riesgo. Usar cuando el usuario pida ver su cuenta, cotizaciones, posiciones, calcular lotes, abrir, modificar o cerrar operaciones, o detener el trading. Toda orden exige stop loss y confirmación explícita del usuario.
---

# Operar en MetaTrader 5 (servidor MCP `mt5`)

El servidor `mt5` aplica los límites de `risk.toml` en código: tú propones, él decide. Tu trabajo es
seguir este flujo, mostrar números claros y no enviar nada sin confirmación explícita del usuario.

## Antes de cualquier operación

1. Llama a `account_info` y `risk_status`.
2. Di en una línea: cuenta, modo (DEMO o REAL), equity, P&L de hoy y margen que queda antes del límite diario.
3. Si `halted` es true, si `algo_trading_enabled` es false, o si `can_open_new_positions` es false: explica el motivo y **no intentes abrir**. No borres `HALT` ni edites `risk.toml`: eso lo hace el usuario a mano.
4. Si el modo es REAL, recuérdalo en cada resumen de orden.

## Abrir una posición

1. `quote(symbol)` y, si hace falta, `symbol_info(symbol)`.
2. El usuario debe dar el **stop loss** (precio). Si no lo da, pídelo o propón uno justificado por estructura o ATR, y espera su respuesta. Sin SL no hay orden.
3. `position_size(symbol, side, sl)` para el volumen por riesgo, o `order_check(...)` para la evaluación completa (riesgo, límites, `order_check` del broker).
4. Muestra el resumen en una tabla: símbolo, lado, precio, volumen, SL, TP, puntos al SL, riesgo en divisa y en % del equity, spread, avisos.
5. Pregunta literalmente: **"¿Confirmas enviar esta orden? Responde CONFIRMO."**
6. Solo si el usuario responde confirmando en su propio mensaje, llama a `order_send(..., confirm=true)` con exactamente los mismos parámetros que mostraste.
7. Informa el resultado (`retcode_name`, ticket, precio de ejecución) y llama a `positions` para verificar.

## Modificar o cerrar

- `modify_position(ticket, sl, tp, confirm=true)` solo tras confirmación. Nunca se quita el SL.
- `close_position(ticket, confirm=true)` solo tras confirmación. Cerrar siempre está permitido, incluso con `HALT`.
- Para cerrar todo de emergencia: `halt_trading(reason, close_all=true)`; avisa que reanudar exige borrar `HALT` a mano.

## Reglas que no se negocian

- Nunca `confirm=true` sin una confirmación explícita del usuario en ese mismo intercambio. Una confirmación vale para una sola orden.
- Nunca subas el volumen, acerques el SL o abras otra posición "para recuperar" pérdidas.
- Si `order_send` devuelve un error (requote, mercado cerrado, sin dinero): informa y pregunta. No reintentes en bucle.
- Si el motor de riesgo rechaza una orden, explica cada razón al usuario; no busques rodeos.
- No prometas resultados. Ningún objetivo de ganancia diaria está garantizado; el objetivo operativo es respetar los límites.
- Registra el contexto de la decisión (setup, motivo) en el `comment` de la orden (máximo 31 caracteres) y resume la operación en el chat; el journal técnico lo escribe el servidor en `journal/trades.jsonl`.

## Diagnóstico rápido

| Síntoma | Causa probable | Qué decir |
|---|---|---|
| `initialize() falló (-10005...)` | MT5 cerrado o sin sesión; varias instalaciones | Abrir MT5 con la cuenta; definir `MT5_TERMINAL_PATH` |
| `AutoTrading está desactivado` | Botón "Algo Trading" apagado | Pulsarlo en la barra de MT5 |
| `order_send` devuelve `TRADE_DISABLED` o `CLIENT_DISABLES_AT` | Opción de MT5 "Deshabilitar el trading automático a través de la API externa de Python" marcada | El usuario la desmarca en Herramientas > Opciones > Asesores Expertos |
| `cuenta ... es REAL y allow_live=false` | Protección por defecto | Solo el usuario puede cambiar `allow_live` en `risk.toml` |
| `trading detenido: existe el archivo HALT` | Kill switch activo | El usuario borra `HALT` cuando decida reanudar |
| `símbolo X no existe en este broker` | Nombre distinto (p. ej. `EURUSD.m`) | Pedir el nombre exacto del broker y, si procede, añadirlo a `symbols` |
