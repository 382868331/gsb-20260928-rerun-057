from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class DependencyType(StrEnum):
    DIRECT = "direct"
    TRANSITIVE = "transitive"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DependencyEvidence:
    ecosystem: str
    name: str
    version: str
    display_name: str = ""
    purl: str | None = None
    dependency_type: DependencyType = DependencyType.UNKNOWN
    requested_by: str | None = None
    scope: str | None = None
    location_path: str = ""
    aliases: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    def normalized(self) -> "DependencyEvidence":
        display_name = self.display_name or self.name
        location = self.location_path or display_name
        return DependencyEvidence(
            ecosystem=self.ecosystem,
            name=self.name,
            version=self.version,
            display_name=display_name,
            purl=self.purl,
            dependency_type=DependencyType(self.dependency_type),
            requested_by=self.requested_by,
            scope=self.scope,
            location_path=location,
            aliases=self.aliases,
            raw=self.raw,
        )


@dataclass(frozen=True)
class ParsedManifest:
    project: str
    project_version: str
    source_path: str
    manifest_type: str
    evidence: tuple[DependencyEvidence, ...]

