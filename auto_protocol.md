# Protocolo de un ciclo del modo automático

Este documento es lo que recibe Claude cada vez que el bucle (`python -m mt5_mcp.autoloop`) lanza un ciclo.
Solo se usa cuando el usuario ha activado el modo automático a mano en `risk.toml` y ha lanzado el bucle.
En cualquier otra conversación se opera con confirmación del usuario para cada orden.

Un ciclo es: mirar el estado, aplicar las reglas de `strategy.md`, actuar si procede, resumir y terminar.
El servidor `mt5` aplica todos los límites en código y puede rechazar cualquier acción. No operar es un
resultado normal del ciclo, y la mayoría de los ciclos terminan sin operar.

## Reglas fijas

- Para actuar se usan solo `auto_order`, `auto_close` y `auto_tighten_stop`. Nunca `order_send`,
  `close_position`, `modify_position` ni `halt_trading`: exigen una confirmación humana que aquí no existe.
- Todos los números salen de las herramientas (`auto_status`, `bars`, `quote`). No estimes precios ni indicadores.
- Sigue `strategy.md` al pie de la letra. Si una condición no se cumple, es dudosa o falta un dato, no se opera.
- Como máximo UNA entrada nueva por ciclo.
- Si una herramienta devuelve `error`, `status: rejected`, `status: failed`, `status: unknown`, o `sent`, `closed` o
  `modified` en false, no reintentes ni cambies los niveles para que pase: anótalo en el resumen y no abras nada más
  en este ciclo. Con `unknown` el terminal no confirmó el resultado: llama a `positions` y di en el resumen si la
  orden llegó a ejecutarse. Si `auto_order` trae tickets en `position_found`, el servidor ya anotó esa posición nueva
  como del bot: cuenta para el cupo y el enfriamiento.
- `journal_error` en una respuesta con `sent`, `closed` o `modified` en true no es un fallo de la orden: la acción
  se hizo y solo falló el registro. Anótalo en el resumen.
- Si `can_open` es false no busques entradas, sea cual sea el motivo.
- No actúes sobre posiciones que no abrió el bot. El servidor lo impide; no lo intentes.
- No edites archivos, no propongas subir límites y no prometas resultados.

## Pasos

1. Llama a `auto_status`.
   - Si `enabled` es false: di que el modo automático está desactivado (solo el usuario lo activa en su
     configuración) y termina.
   - Anota `can_open`, `can_manage_positions`, `blockers` y `open_auto_positions`.
2. **Gestión.** Si `can_manage_positions` es true y hay posiciones en `open_auto_positions`, aplica a cada una las
   reglas de gestión de la estrategia con los datos de `bars` de su símbolo. Usa `auto_tighten_stop` o `auto_close`
   con un `reason` que cite la regla que se cumple y los valores que la cumplen.
3. **Entrada.** Solo si `can_open` es true. Recorre los símbolos de `limits.symbols` en ese orden, saltando los que
   ya tienen una posición abierta. Para cada uno pide las velas que indica la estrategia y comprueba TODAS las
   condiciones. En el primer símbolo que las cumpla, calcula SL y TP como dice la estrategia y llama a
   `auto_order(symbol, side, sl, tp, reason)`. El `reason` cita las condiciones con sus valores. Después de una
   llamada a `auto_order`, haya salido bien o mal, deja de buscar entradas.
4. **Resumen** en 3 a 6 líneas: estado de la cuenta y del bot, lo que hiciste con su ticket, o la condición que
   falló en cada símbolo. Sin recomendaciones ni pronósticos.
