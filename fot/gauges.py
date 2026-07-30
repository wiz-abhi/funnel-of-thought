"""Re-emit funnel analytics as OpenTelemetry gauges.

Why this module exists: SigNoz's trace-funnel analytics live behind a bespoke REST
endpoint. They are not a metric, so they cannot be put on a dashboard, cannot be
alerted on, and cannot be compared over time. Reading the funnel and re-emitting
each step's conversion as a plain OTLP gauge closes that gap -- once the numbers
are metrics, the whole rest of the platform works on them.

Emitted instruments:

* ``fot.funnel.step.conversion`` -- percent of entering traces that reached the step
* ``fot.funnel.step.n``          -- absolute traces that reached the step
* ``fot.funnel.step.step_conversion`` -- percent relative to the *previous* step

All three carry attributes ``funnel``, ``step``, ``step_order``, and ``service``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

from .funnels import Funnel
from .signoz import StepCounts

__all__ = ["GaugeEmitter", "EmitResult", "DEFAULT_OTLP_ENDPOINT"]

DEFAULT_OTLP_ENDPOINT = os.environ.get(
    "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"
).rstrip("/")

CONVERSION_GAUGE = "fot.funnel.step.conversion"
COUNT_GAUGE = "fot.funnel.step.n"
STEP_CONVERSION_GAUGE = "fot.funnel.step.step_conversion"


@dataclass(slots=True)
class EmitResult:
    """Summary of one emission pass.

    Attributes:
        points: number of individual gauge data points recorded.
        endpoint: OTLP metrics URL they were shipped to.
        series: ``(step_label, n, cumulative_pct)`` per step, for CLI display.
    """

    points: int
    endpoint: str
    series: list[tuple[str, int, float]]


class GaugeEmitter:
    """Ships funnel step conversions to an OTLP/HTTP metrics endpoint.

    Uses observable (async) gauges backed by a snapshot dict: values are staged by
    :meth:`record`, then a forced flush triggers the callbacks exactly once, so a
    single CLI invocation produces exactly one data point per series.

    Example:
        >>> emitter = GaugeEmitter()                      # doctest: +SKIP
        >>> emitter.record(funnel, counts)                # doctest: +SKIP
        >>> emitter.flush()                               # doctest: +SKIP
    """

    def __init__(
        self,
        endpoint: str = DEFAULT_OTLP_ENDPOINT,
        *,
        service_name: str = "fot-cli",
        timeout: int = 15,
    ) -> None:
        """Configure the exporter.

        Args:
            endpoint: OTLP base URL; ``/v1/metrics`` is appended if absent.
            service_name: ``service.name`` stamped on the emitted resource.
            timeout: export timeout in seconds.
        """
        self.endpoint = endpoint if endpoint.endswith("/v1/metrics") else f"{endpoint}/v1/metrics"
        self._snapshot: dict[tuple[str, str, str], tuple[float, float, float, int]] = {}

        exporter = OTLPMetricExporter(endpoint=self.endpoint, timeout=timeout)
        # A long interval keeps the periodic reader out of the way; we always flush
        # explicitly so the CLI stays synchronous and predictable.
        self._reader = PeriodicExportingMetricReader(
            exporter, export_interval_millis=600_000, export_timeout_millis=timeout * 1000
        )
        self._provider = MeterProvider(
            resource=Resource.create({"service.name": service_name}),
            metric_readers=[self._reader],
        )
        meter = self._provider.get_meter("fot.funnels")

        meter.create_observable_gauge(
            CONVERSION_GAUGE,
            callbacks=[lambda _o: self._observe(0)],
            unit="%",
            description="Percent of traces entering the funnel that reached this step, in order",
        )
        meter.create_observable_gauge(
            STEP_CONVERSION_GAUGE,
            callbacks=[lambda _o: self._observe(1)],
            unit="%",
            description="Percent of traces reaching the previous step that reached this step",
        )
        meter.create_observable_gauge(
            COUNT_GAUGE,
            callbacks=[lambda _o: self._observe(2)],
            unit="1",
            description="Absolute traces that reached this funnel step, in order",
        )

    def _observe(self, slot: int):
        """Yield staged observations for one instrument.

        Args:
            slot: 0 = cumulative conversion, 1 = step conversion, 2 = absolute n.
        """
        from opentelemetry.metrics import Observation

        out = []
        for (funnel_name, label, service), values in self._snapshot.items():
            out.append(
                Observation(
                    values[slot],
                    {
                        "funnel": funnel_name,
                        "step": label,
                        "step_order": values[3],
                        "service": service,
                    },
                )
            )
        return out

    def record(self, funnel: Funnel, counts: StepCounts) -> list[tuple[str, int, float]]:
        """Stage one funnel's step values for the next flush.

        Args:
            funnel: definition supplying names and per-step services.
            counts: analytics counts to publish.

        Returns:
            ``(label, n, cumulative_pct)`` per step, for display.
        """
        cumulative = counts.cumulative_pct()
        per_step = counts.step_pct()
        series: list[tuple[str, int, float]] = []
        for i, label in enumerate(counts.labels):
            service = funnel.steps[i].service if i < len(funnel.steps) else funnel.service
            self._snapshot[(funnel.name, label, service)] = (
                cumulative[i], per_step[i], float(counts.totals[i]), i + 1
            )
            series.append((label, counts.totals[i], cumulative[i]))
        return series

    def flush(self) -> int:
        """Force an export of everything staged.

        Returns:
            Number of gauge data points shipped (3 per step).

        Raises:
            RuntimeError: if the OTLP collector rejects or cannot be reached.
        """
        points = len(self._snapshot) * 3
        if not self._provider.force_flush(timeout_millis=20_000):
            raise RuntimeError(
                f"OTLP export to {self.endpoint} did not complete; is the collector listening?"
            )
        # shutdown() runs one final collection, which re-fires every observable
        # callback over this same snapshot and reports each series twice. Gauges
        # are last-write-wins so the panels still looked correct, but any
        # sum/rate/count widget read double.
        self._snapshot.clear()
        return points

    def shutdown(self) -> None:
        """Flush and tear down the meter provider."""
        self._provider.shutdown()
