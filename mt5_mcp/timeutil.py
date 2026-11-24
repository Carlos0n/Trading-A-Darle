"""Hora del servidor de trading.

MetaTrader 5 entrega los tiempos de ticks, velas y operaciones como segundos "desde 1970" en el reloj del
SERVIDOR del broker (habitualmente UTC+2/UTC+3), no en UTC real, y la API de Python no expone la hora del
servidor. Aquí se estima el desfase a partir de los ticks, con estas reglas:

- Mercado vivo: solo cuenta un tick cuyo tiempo ha CAMBIADO respecto a una lectura hecha poco antes. Una
  cotización parada hace k x 30 minutos es indistinguible, con una sola lectura, de una recién llegada de un
  servidor con otro huso; un tick que se mueve entre dos lecturas cercanas sí acaba de llegar.
- Dos ticks, no uno: un tick también puede "moverse" una sola vez sin ser nuevo (al reconectar tras suspender
  el PC, el terminal sustituye su último tick por el último del servidor, que puede llevar horas parado). Por
  eso un desfase nuevo o menor que el vigente exige que el símbolo ya estuviera cotizando (su tick anterior
  era reciente) o que un segundo movimiento diga lo mismo. Un mercado vivo lo cumple enseguida.
- Causalidad: un tick no puede venir del futuro. Si alguno supera la hora que da el desfase guardado, ese
  desfase ya no vale (el servidor adelantó la hora) y se vuelve a medir.
"""

from __future__ import annotations

from dataclasses import dataclass, field

GRID = 1800  # los husos de los servidores son múltiplos de 30 minutos
MAX_BEHIND = 120  # el tick más fresco puede tener hasta 2 minutos; también es la separación máxima entre dos lecturas
MAX_AHEAD = 30  # tolerancia al desajuste del reloj del PC
MIN_OFFSET = -12 * 3600
MAX_OFFSET = 14 * 3600
CACHE_TTL = 12 * 3600  # sin evidencia fresca en 12 h, el desfase guardado deja de usarse


def candidate_offset(tick_times: list[int], utc_now: float) -> int | None:
    """Desfase servidor-UTC (segundos) que explica el tick más fresco, o None si ninguno es reciente.

    Por sí solo no distingue un tick recién llegado de uno parado hace un múltiplo de 30 minutos: quien llama
    debe pasar solo ticks que haya visto moverse (ver ServerClock.update).
    """
    times = [int(t) for t in tick_times if t and t > 0]
    if not times:
        return None
    raw = max(times) - utc_now
    rounded = int(round(raw / GRID)) * GRID
    residual = raw - rounded
    if not (-MAX_BEHIND <= residual <= MAX_AHEAD):
        return None
    if not (MIN_OFFSET <= rounded <= MAX_OFFSET):
        return None
    return rounded


@dataclass
class ServerClock:
    """Guarda el último desfase confirmado y la lectura anterior de ticks. `known` indica si hay desfase fiable."""

    offset: int | None = None
    confirmed_at: float = 0.0
    prev: dict[str, int] = field(default_factory=dict)  # lectura anterior: símbolo -> tiempo de su último tick
    prev_at: float | None = None  # hora UTC de esa lectura
    moved_at: float | None = None  # última lectura (UTC) en la que algún tick había cambiado
    pending: int | None = None  # desfase que propone un único movimiento, a la espera de que otro lo confirme
    pending_symbols: set[str] = field(default_factory=set)  # símbolos cuyo movimiento propuso `pending`
    seen_moving: dict[str, float] = field(default_factory=dict)  # símbolo -> lectura (UTC) tras la cual se vio cambiar su tick por última vez

    def has_recent_reading(self, utc_now: float) -> bool:
        """¿Hay una lectura anterior lo bastante cercana como para comparar con ella?"""
        return self.prev_at is not None and 0 <= utc_now - self.prev_at <= MAX_BEHIND

    def update(self, ticks: dict[str, int], utc_now: float) -> int | None:
        """Incorpora una lectura {símbolo: tiempo del último tick} hecha en `utc_now` y devuelve el desfase."""
        recent = self.has_recent_reading(utc_now)
        # "distinto", no "mayor": cuando el servidor atrasa la hora, el tiempo del tick nuevo retrocede
        moved = [s for s, t in ticks.items() if recent and s in self.prev and t != self.prev[s]]
        if not recent:
            self.pending = None  # la confirmación debe llegar dentro de la misma serie de lecturas cercanas
            self.pending_symbols = set()
        if self.offset is not None and utc_now - self.confirmed_at > CACHE_TTL:
            self.offset = None  # caducado
        cand = candidate_offset([ticks[s] for s in moved], utc_now)
        if cand is not None:
            if self._accepts(cand, moved):
                self.offset, self.confirmed_at, self.pending = cand, utc_now, None
                self.pending_symbols = set()
            else:
                if cand != self.pending:
                    self.pending_symbols = set()
                self.pending = cand
                self.pending_symbols.update(moved)
        elif self.offset is not None and not recent and max(ticks.values(), default=0) > utc_now + self.offset + MAX_AHEAD:
            # En una lectura aislada no se puede ver moverse nada: si aun así un tick va por delante de la hora
            # que da el desfase guardado, el desfase está anticuado y hay que volver a medirlo.
            self.offset = None
        if moved:
            self.moved_at = utc_now
            # el tick nuevo llegó al terminal después de la lectura anterior: eso acota cuánto hace que llegó
            self.seen_moving.update(dict.fromkeys(moved, self.prev_at))
        self.prev, self.prev_at = dict(ticks), utc_now
        return self.offset

    def _accepts(self, cand: int, moved: list[str]) -> bool:
        """¿Basta este movimiento para fiarse de `cand`? (se llama antes de guardar la lectura nueva)"""
        if self.offset is not None and cand >= self.offset:
            return True  # confirma el desfase vigente, o un tick "del futuro" obliga a subirlo
        if cand == self.pending and self.pending_symbols.intersection(moved):
            # segundo movimiento del MISMO símbolo: al resincronizar, el terminal mueve cada símbolo una sola vez, así
            # que dos símbolos distintos que cambian en lecturas seguidas no prueban que el mercado esté vivo
            return True
        # El símbolo ya estaba cotizando: en la lectura anterior su tick era reciente, medido con el desfase
        # vigente (el servidor acaba de atrasar su reloj) o con el propuesto (dos ticks frescos seguidos).
        for symbol in moved:
            for offset in (self.offset, cand):
                if offset is not None and -MAX_AHEAD <= self.prev_at + offset - self.prev[symbol] <= MAX_BEHIND:
                    return True
        return False

    @property
    def known(self) -> bool:
        return self.offset is not None

    def is_live(self, utc_now: float) -> bool:
        """¿Se ha visto moverse algún tick hace poco? (llegan cotizaciones, se conozca o no el desfase)"""
        return self.moved_at is not None and 0 <= utc_now - self.moved_at <= MAX_BEHIND

    def arrived_within(self, symbol: str, utc_now: float) -> float | None:
        """Segundos, como mucho, que hace que el tick actual de `symbol` llegó al terminal; None si no se sabe.

        Se sabe cuando se le ha visto cambiar entre dos lecturas: llegó después de la primera. No dice que el tick
        sea nuevo en el servidor (al reconectar, el terminal recibe de golpe el último, por viejo que sea).
        """
        since = self.seen_moving.get(symbol)
        return None if since is None or utc_now < since else utc_now - since

    def server_now(self, utc_now: float) -> float | None:
        """Hora actual en el reloj del servidor; None si el desfase no se conoce (nunca se supone UTC)."""
        return None if self.offset is None else utc_now + self.offset

    def age(self, stamp: int, utc_now: float) -> float | None:
        """Antigüedad en segundos de un tiempo del servidor; None si el desfase no se conoce."""
        if self.offset is None or not stamp:
            return None
        return max(0.0, utc_now + self.offset - stamp)
