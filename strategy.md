# Estrategia del bot

> Plantilla de ejemplo para probar el sistema en cuenta DEMO. No está validada con datos históricos y no
> garantiza ganancias: puede perder. El bot sigue lo que diga este archivo, así que cámbialo con tus reglas.
> Los límites de riesgo no están aquí: están en `risk.toml` y los aplica el servidor.

Idea: operar a favor de la tendencia de una hora, entrando cuando el precio retrocede a su media en 15 minutos.

## Datos

Para cada símbolo se piden dos lecturas:

- `bars(symbol, "H1", 5)`: tendencia de fondo, en `indicators.trend`.
- `bars(symbol, "M15", 5)`: entrada. Se usan `indicators.rsi14`, `indicators.atr14`, `indicators.atr14_points`,
  `indicators.close_minus_ema20_in_atr`, `last_closed_bar` y `quote`.

## Cuándo no operar

No se abre nada en un símbolo si ocurre cualquiera de estas cosas en la lectura M15:

- `quote.stale` es true.
- `indicators.atr14` o `indicators.rsi14` es null.
- `quote.spread_points` es mayor que 0,2 × `indicators.atr14_points`.

## Compra

Deben cumplirse las cuatro condiciones:

1. H1: `indicators.trend` es `"up"`.
2. M15: `indicators.close_minus_ema20_in_atr` está entre −0,6 y +0,4.
3. M15: `indicators.rsi14` está entre 40 y 60.
4. M15: la última vela cerrada es alcista: `last_closed_bar.close` mayor que `last_closed_bar.open`.

Niveles, con `quote.ask` como precio de entrada y redondeando a `quote.digits` decimales:

- SL = ask − 1,5 × `atr14`
- TP = ask + 3 × `atr14`

## Venta

Deben cumplirse las cuatro condiciones:

1. H1: `indicators.trend` es `"down"`.
2. M15: `indicators.close_minus_ema20_in_atr` está entre −0,4 y +0,6.
3. M15: `indicators.rsi14` está entre 40 y 60.
4. M15: la última vela cerrada es bajista: `last_closed_bar.close` menor que `last_closed_bar.open`.

Niveles, con `quote.bid` como precio de entrada:

- SL = bid + 1,5 × `atr14`
- TP = bid − 3 × `atr14`

## Gestión de posiciones abiertas

Para cada posición del bot, con sus datos de `auto_status` (`price_open`, `price_current`, `sl`) y la lectura H1:

- **Proteger.** Si el SL sigue en zona de pérdida y el precio ha avanzado a favor al menos la distancia inicial
  al SL, mueve el SL al precio de entrada con `auto_tighten_stop`.
  - Compra: `sl` menor que `price_open` y `price_current − price_open` mayor o igual que `price_open − sl`.
  - Venta: `sl` mayor que `price_open` y `price_open − price_current` mayor o igual que `sl − price_open`.
- **Salir antes.** Si la tendencia H1 se ha dado la vuelta contra la posición, ciérrala con `auto_close`.
  - Compra con `indicators.trend` igual a `"down"`. Venta con `indicators.trend` igual a `"up"`.
- En cualquier otro caso no se toca nada: el SL y el TP ya están en el broker.
