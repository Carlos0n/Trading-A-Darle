"""Hora del servidor de trading.

MetaTrader 5 entrega los tiempos de ticks, velas y operaciones como segundos "desde 1970" en el reloj del
SERVIDOR del broker (habitualmente UTC+2/UTC+3), no en UTC real, y la API de Python no expone la hora del
servidor. Aquí se estima el desfase a partir de los ticks más recientes, con una regla de causalidad:
un tick no puede venir del futuro, así que el desfase real es >= (tiempo_del_tick - UTC_real).
"""

from __future__ import annotations

from dataclasses import dataclass

GRID = 1800  # los husos de los servidores son múltiplos de 30 minutos
MAX_BEHIND = 120  # el tick más fresco puede tener hasta 2 minutos
MAX_AHEAD = 30  # tolerancia al desajuste del reloj del PC
MIN_OFFSET = -12 * 3600
MAX_OFFSET = 14 * 3600
CACHE_TTL = 12 * 3600  # sin evidencia fresca en 12 h, el desfase guardado deja de usarse


def candidate_offset(tick_times: list[int], utc_now: float) -> int | None:
    """Desfase servidor-UTC (segundos) que explica el tick más fresco, o None si ninguno es reciente."""
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
    """Guarda el último desfase fiable. `known` indica si hay evidencia reciente."""

    offset: int | None = None
    confirmed_at: float = 0.0

    def update(self, tick_times: list[int], utc_now: float) -> int | None:
        cand = candidate_offset(tick_times, utc_now)
        cached_ok = self.offset is not None and (utc_now - self.confirmed_at) <= CACHE_TTL
        if cand is not None:
            if not cached_ok or cand >= self.offset:
                # sin caché válida, o el candidato es mayor: por causalidad el mayor es el correcto
                self.offset = cand
                self.confirmed_at = utc_now
            # cand < offset guardado: los ticks están atrasados (mercado parado); se conserva el guardado
        elif not cached_ok:
            self.offset = None
        return self.offset

    @property
    def known(self) -> bool:
        return self.offset is not None

    def server_now(self, utc_now: float) -> float:
        return utc_now + (self.offset or 0)

    def age(self, stamp: int, utc_now: float) -> float | None:
        """Antigüedad en segundos de un tiempo del servidor; None si el desfase no se conoce."""
        if self.offset is None or not stamp:
            return None
        return max(0.0, self.server_now(utc_now) - stamp)
