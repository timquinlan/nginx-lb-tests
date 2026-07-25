"""Ant Colony Optimization algorithm module.

One pheromone float per backend. Each sampling window: evaporate every
backend's pheromone by a configurable rate, then deposit an amount
inversely proportional to that backend's observed mean latency (lower
latency -> bigger deposit -> more reinforcement -- "higher latency = less
reinforcement" from the design doc). Weights are then read off the
pheromone table relative to its current max, not its sum -- see
AGENT.md "ACO weight conversion" for why.
"""
import os

from algorithms.base import Algorithm

# Retains (1 - rate) of prior pheromone each window. Low rate = slow to
# forget = stable but laggy on sudden latency shifts, which is the
# behavioral contrast with Markov (Phase 3) the design doc calls for.
EVAPORATION_RATE = float(os.environ.get("ACO_EVAPORATION_RATE", "0.1"))

# Deposit = DEPOSIT_CONSTANT / latency_ms. At the default 0.1 evaporation
# rate this constant puts steady-state pheromone for a ~20ms backend at
# roughly 4x that of an ~80ms backend -- enough separation to visibly shift
# weights within a handful of windows without being so aggressive that a
# single noisy observation swings weights wildly. Tune via env var if a
# given backend pool needs faster/slower convergence.
DEPOSIT_CONSTANT = float(os.environ.get("ACO_DEPOSIT_CONSTANT", "10.0"))

INITIAL_PHEROMONE = 1.0
MIN_LATENCY_MS = 0.001  # guards divide-by-zero on a degenerate 0ms observation


class AntColonyOptimization(Algorithm):
    def __init__(self):
        self.pheromone = {}

    def update(self, observations: dict) -> dict:
        for host in observations:
            self.pheromone.setdefault(host, INITIAL_PHEROMONE)

        for host, latency_ms in observations.items():
            # Evaporate first, then deposit -- this window's reinforcement
            # isn't discounted by this same window's decay.
            self.pheromone[host] *= (1.0 - EVAPORATION_RATE)
            self.pheromone[host] += DEPOSIT_CONSTANT / max(latency_ms, MIN_LATENCY_MS)

        return self._weights_from_pheromone()

    def _weights_from_pheromone(self):
        # Scaled relative to the current max, not normalized to sum to 100.
        # Sum-based scaling gets coarser as the backend count grows (each
        # share shrinks toward 1/N); max-relative scaling keeps the top
        # backend at ~100 and everything else proportional to it regardless
        # of how many backends are in the pool. See AGENT.md Scaling.
        max_pheromone = max(self.pheromone.values())
        if max_pheromone <= 0:
            return {host: self.clamp(50) for host in self.pheromone}
        return {
            host: self.clamp(100 * level / max_pheromone)
            for host, level in self.pheromone.items()
        }
