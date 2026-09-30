from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass(frozen=True)
class Component:
    """A component observed by a scan. Immutable once recorded."""

    name: str
    ecosystem: str
    version: str
    source: str  # project / repository that produced the observation
    build_path: str  # dependency path through which it was reached
    scan_id: str

    @property
    def key(self) -> tuple:
        return (self.name, self.ecosystem, self.version, self.source, self.build_path)


@dataclass(frozen=True)
class AliasDecision:
    """A human decision that two names refer to the same component.

    The decision only applies inside its explicit scope: one ecosystem,
    a version range, and a set of sources. Anything outside the scope is
    never merged.
    """

    decision_id: str
    name_a: str
    name_b: str
    ecosystem: str
    version_range: str  # e.g. ">=1.0.0,<2.0.0" or "*"
    sources: tuple  # tuple of source ids, or ("*",) for all
    status: str = "pending"  # pending | approved | withdrawn
    created_at: str = ""
    resolved_at: Optional[str] = None

    def matches_source(self, source: str) -> bool:
        return self.sources == ("*",) or source in self.sources


@dataclass(frozen=True)
class ConflictEvidence:
    """Recorded divergence evidence. Kept visible even after aliasing."""

    evidence_id: str
    name: str
    ecosystem: str
    source: str
    versions: tuple
    created_at: str


def to_dict(obj) -> dict:
    d = asdict(obj)
    for k, v in d.items():
        if isinstance(v, tuple):
            d[k] = list(v)
    return d
