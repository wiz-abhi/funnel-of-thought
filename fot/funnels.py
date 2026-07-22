"""Funnel definitions as code.

A *reasoning contract* is just an ordered list of spans that a well-behaved agent
must emit, in order, inside a single trace. This module loads those contracts from
YAML and applies them to SigNoz, so the funnel that judges your agent lives in
version control next to the agent itself rather than being hand-drawn in a UI.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import yaml

from .signoz import SigNozClient

__all__ = ["Step", "Funnel", "FunnelSuite", "load_suite", "DEFAULT_SUITE_PATH"]

DEFAULT_SUITE_PATH = Path(__file__).resolve().parent / "funnels" / "cognition.yaml"

#: ``${VAR}`` or ``${VAR:-default}`` inside any YAML string value.
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(value: str) -> str:
    """Expand ``${VAR}`` / ``${VAR:-default}`` references in a YAML scalar.

    Lets one checked-in funnel file target a local stack, CI, and prod without
    forking the file -- e.g. ``service: ${FOT_SERVICE:-fot-agent}``.
    """
    return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)


def _walk_expand(node: Any) -> Any:
    """Recursively apply :func:`_expand_env` to every string in a parsed document."""
    if isinstance(node, str):
        return _expand_env(node)
    if isinstance(node, list):
        return [_walk_expand(v) for v in node]
    if isinstance(node, dict):
        return {k: _walk_expand(v) for k, v in node.items()}
    return node


@dataclass(slots=True)
class Step:
    """One node of a reasoning contract.

    Attributes:
        service: OTel ``service.name`` that must emit the span.
        span: exact span name to match.
        label: short human name used in CLI output and metric attributes.
        has_errors: when ``True``, only errored spans satisfy this step.
        latency_type: percentile SigNoz reports for the step.
            NOTE: ``p50`` is silently treated as ``p99`` by the server (the switch
            only implements p90/p95), so this is constrained to p90/p95 to avoid
            reporting a number that is quietly wrong.
    """

    service: str
    span: str
    label: str
    has_errors: bool = False
    latency_type: str = "p95"

    def __post_init__(self) -> None:
        if self.latency_type not in ("p90", "p95"):
            raise ValueError(
                f"latency_type {self.latency_type!r} unsupported; use p90 or p95 "
                "(SigNoz silently returns p99 for p50)"
            )

    def to_payload(self, order: int) -> dict[str, Any]:
        """Render the SigNoz step payload for ``PUT /trace-funnels/steps/update``.

        NOTE: ``id`` is deliberately **omitted** -- SigNoz assigns a UUID, and
        sending ``"id": "1"`` fails with ``invalid UUID length: 1``.
        """
        return {
            "step_order": order,
            "service_name": self.service,
            "span_name": self.span,
            "filters": {"items": [], "op": "AND"},
            "latency_pointer": "start",
            "latency_type": self.latency_type,
            "has_errors": self.has_errors,
            "name": self.label,
        }


@dataclass(slots=True)
class Funnel:
    """A named, ordered reasoning contract.

    Attributes:
        name: funnel name in SigNoz (also the CLI handle).
        description: free text shown in CLI headers.
        steps: ordered steps; order *is* the contract.
    """

    name: str
    description: str
    steps: list[Step]

    @property
    def labels(self) -> list[str]:
        """Step labels in order."""
        return [s.label for s in self.steps]

    @property
    def service(self) -> str:
        """Primary service (that of step 1), used for the naive ClickHouse queries."""
        return self.steps[0].service if self.steps else ""

    def payloads(self) -> list[dict[str, Any]]:
        """All step payloads, 1-indexed as SigNoz expects."""
        return [s.to_payload(i + 1) for i, s in enumerate(self.steps)]

    def apply(self, client: SigNozClient) -> tuple[str, bool]:
        """Create or update this funnel in SigNoz (idempotent by name).

        Returns:
            ``(funnel_id, created)`` where ``created`` is ``False`` if an existing
            funnel of the same name was updated in place.
        """
        existing = client.find_funnel(self.name)
        if existing:
            funnel_id = existing.get("funnel_id") or existing.get("id")
            client.update_steps(funnel_id, self.payloads())
            return funnel_id, False
        funnel_id = client.create_funnel(self.name)
        client.update_steps(funnel_id, self.payloads())
        return funnel_id, True


@dataclass(slots=True)
class FunnelSuite:
    """A YAML file's worth of funnels."""

    path: Path
    funnels: list[Funnel]

    def __iter__(self) -> Iterator[Funnel]:
        return iter(self.funnels)

    def __len__(self) -> int:
        return len(self.funnels)

    def get(self, name: str) -> Funnel:
        """Look up a funnel by name.

        Raises:
            KeyError: with the available names listed, if ``name`` is unknown.
        """
        for funnel in self.funnels:
            if funnel.name == name:
                return funnel
        available = ", ".join(f.name for f in self.funnels) or "<none>"
        raise KeyError(f"no funnel named {name!r} in {self.path.name}; have: {available}")


def load_suite(path: str | Path = DEFAULT_SUITE_PATH) -> FunnelSuite:
    """Parse a funnel-definition YAML file.

    The document shape is::

        funnels:
          - name: cognition
            description: ...
            steps:
              - {service: fot-agent, span: agent.plan, label: plan}

    Args:
        path: YAML file to read.

    Returns:
        A :class:`FunnelSuite`.

    Raises:
        FileNotFoundError: if ``path`` does not exist.
        ValueError: if the document is structurally invalid.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"funnel definition not found: {path}")
    doc = _walk_expand(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    raw_funnels = doc.get("funnels")
    if not isinstance(raw_funnels, list) or not raw_funnels:
        raise ValueError(f"{path}: expected a non-empty top-level 'funnels' list")

    funnels: list[Funnel] = []
    for raw in raw_funnels:
        name = raw.get("name")
        if not name:
            raise ValueError(f"{path}: every funnel needs a 'name'")
        raw_steps = raw.get("steps") or []
        if len(raw_steps) < 2:
            raise ValueError(f"{path}: funnel {name!r} needs at least 2 steps")
        steps = [
            Step(
                service=s["service"],
                span=s["span"],
                label=s.get("label") or s["span"],
                has_errors=bool(s.get("has_errors", False)),
                latency_type=s.get("latency_type", "p95"),
            )
            for s in raw_steps
        ]
        funnels.append(
            Funnel(name=name, description=raw.get("description", ""), steps=steps)
        )
    return FunnelSuite(path=path, funnels=funnels)
