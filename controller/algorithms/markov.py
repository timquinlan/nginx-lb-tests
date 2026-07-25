"""Markov Chain algorithm module.

Transition matrix (backends x backends), rebuilt completely from scratch
every sampling window -- no state carried over between windows. This is
the "memoryless" contrast with ACO's persisted, slowly-decaying pheromone
table (see aco.py): Markov reacts fully to the current window's
observations with no smoothing from prior windows, trading stability for
responsiveness (see AGENT.md "Markov weight update").
"""
from algorithms.base import Algorithm

MIN_LATENCY_MS = 0.001  # guards divide-by-zero on a degenerate 0ms observation
STATIONARY_ITERATIONS = 200
STATIONARY_TOLERANCE = 1e-9


class MarkovChain(Algorithm):
    def update(self, observations: dict) -> dict:
        hosts = list(observations.keys())
        if len(hosts) == 1:
            return {hosts[0]: self.clamp(100)}

        matrix = self._build_transition_matrix(hosts, observations)
        stationary = self._stationary_distribution(hosts, matrix)
        # The stationary distribution already sums to ~1 -- it's a
        # normalized proportion-of-time-in-each-state, so scaling it
        # directly by 100 carries that proportion straight into the weight
        # ratios. That's a different choice than ACO's max-relative scaling
        # (see aco.py): pheromone is an unbounded accumulator best read
        # relative to the current leader, a stationary distribution is
        # already normalized and is best read by direct scaling.
        return {host: self.clamp(100 * p) for host, p in stationary.items()}

    def _build_transition_matrix(self, hosts, observations):
        # Never self-transition -- every window moves to a (possibly
        # different) backend, weighted toward whichever others had lower
        # observed latency. Excluding i from i's own row normalization
        # makes each row depend on the *other* backends' relative scores,
        # so this is genuine per-state transition probabilities rather
        # than N identical rows (which would trivialize the "matrix" down
        # to a single vector).
        scores = {h: 1.0 / max(observations[h], MIN_LATENCY_MS) for h in hosts}
        matrix = {}
        for i in hosts:
            others_total = sum(scores[k] for k in hosts if k != i)
            matrix[i] = {j: (scores[j] / others_total if j != i else 0.0) for j in hosts}
        return matrix

    def _stationary_distribution(self, hosts, matrix):
        n = len(hosts)
        pi = {h: 1.0 / n for h in hosts}
        for _ in range(STATIONARY_ITERATIONS):
            next_pi = {h: 0.0 for h in hosts}
            for i in hosts:
                for j in hosts:
                    next_pi[j] += pi[i] * matrix[i][j]
            diff = sum(abs(next_pi[h] - pi[h]) for h in hosts)
            pi = next_pi
            if diff < STATIONARY_TOLERANCE:
                break
        return pi
