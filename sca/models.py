from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ComponentCoordinate:
    ecosystem: str
    canonical_name: str
    display_name: str
    purl: str | None = None


@dataclass(frozen=True)
class DependencyRecord:
    source_path: str
    index: int
    display_name: str
    coordinate: ComponentCoordinate
    version: str | None
    version_spec: str | None
    dependency_type: str
    artifact_path: str
    dependency_chain: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedManifest:
    source_path: str
    format: str
    content_hash: str
    project_name: str
    project_version: str
    records: tuple[DependencyRecord, ...]
