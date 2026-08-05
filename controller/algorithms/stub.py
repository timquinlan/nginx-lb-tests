"""Phase 1 placeholder algorithm: equal weights, no learning.

Standing instructions are explicit that ACO/Markov math must not be
implemented until Phase 1 scaffolding (validation, config generation,
priming, the sampling-loop cadence, the config writer, the change counter)
is working end-to-end. This module lets the dynamic algorithm paths exercise that full
plumbing in Phase 1 without pretending to implement real algorithm logic --
weights stay equal every window, so the change counter should stay at 1
(the one change from the startup placeholder to the primed value) for the
life of the run.
"""
from algorithms.base import Algorithm
from common import EQUAL_WEIGHT_PLACEHOLDER


class EqualWeightStub(Algorithm):
    def update(self, observations: dict) -> dict:
        return {host: EQUAL_WEIGHT_PLACEHOLDER for host in observations}
