"""Algorithm module interface.

Every upstream-selection algorithm (other than round robin, which is static
and has no module) implements this interface. Adding a new algorithm is:

  1. Subclass Algorithm, implement update()
  2. Add one upstream conf file + one location block (see controller/nginx/)
  3. Wire it into sampler.py's per-algorithm loop list

Nothing else changes -- sampler.py, config_writer.py, and the traffic
generator are all algorithm-agnostic.
"""
from abc import ABC, abstractmethod

from common import MIN_WEIGHT, MAX_WEIGHT


class Algorithm(ABC):
    @abstractmethod
    def update(self, observations: dict) -> dict:
        """observations: {backend_host: mean_latency_ms} for every backend
        with at least one observation this window (sparse hosts are
        backfilled by sampler.py's fallback probe before this is called, so
        every known backend is always present).

        Returns {backend_host: integer_weight} with every weight clamped to
        [MIN_WEIGHT, MAX_WEIGHT] (1-100). Must return a weight for every host
        it was given, plus any it has seen in a prior call for this run.
        """
        raise NotImplementedError

    @staticmethod
    def clamp(weight):
        return max(MIN_WEIGHT, min(MAX_WEIGHT, int(round(weight))))
