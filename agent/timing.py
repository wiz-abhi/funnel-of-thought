"""Simulated per-node latency.

THESE SLEEPS ARE LOAD-BEARING. DO NOT REMOVE THEM.

A SigNoz Trace Funnel computes ``minIf(timestamp, ...)`` per step and requires
each step's timestamp to be STRICTLY greater than the previous step's. Mocked
nodes that do no real work complete within the same clock tick, so all four
spans land on an identical timestamp with ``duration_nano = 0``. When that
happens the strict ``t2 > t1`` comparison fails, per-step trace counts collapse
(we measured 6/100 instead of 100/100) and ``/analytics/overview`` returns
HTTP 500 because the conversion rate is NaN.

Verified fix: give every node a non-trivial amount of simulated work. Live
Gemini calls take 1-2s and are safe on their own; it is specifically --stub and
the mocked search tool that are at risk.

Ranges below are randomized to make the trace waterfall look realistic.
``--fast`` scales them down for quick smoke tests but NEVER to zero -- the
floor is enforced by :data:`MIN_SLEEP_S`.
"""

from __future__ import annotations

import random
import time

#: Hard floor, in seconds. Below roughly this, spans risk sharing a timestamp.
MIN_SLEEP_S = 0.004

#: node -> (low, high) simulated duration in seconds, at scale 1.0.
NODE_LATENCY_S: dict[str, tuple[float, float]] = {
    "plan": (0.200, 0.600),
    "tool": (0.100, 0.400),
    "validate": (0.050, 0.200),
    "respond": (0.200, 0.600),
}

#: Multiplier applied by --fast. Chosen so the slowest node still clears the floor.
FAST_SCALE = 0.02


class Timing:
    """Per-run latency profile."""

    def __init__(self, scale: float = 1.0):
        self.scale = scale

    def sleep_for(self, node: str, rng: random.Random) -> float:
        """Block for this node's simulated work. Returns the seconds slept."""
        low, high = NODE_LATENCY_S.get(node, (0.050, 0.150))
        seconds = max(MIN_SLEEP_S, rng.uniform(low, high) * self.scale)
        time.sleep(seconds)
        return seconds
