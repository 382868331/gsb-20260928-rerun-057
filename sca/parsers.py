from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import DependencyEvidence, DependencyType, ParsedManifest


class ManifestError(ValueError):
    pass


def _dependency_type(value: Any) -> DependencyType:
    if value in ("direct", "transitive", "unknown"):
        return DependencyType(value)
    return DependencyType.UNKNOWN


def _dependencies(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def parse_pip_lock(path: Path) -> ParsedManifest:
    data = json.loads(path.read_text(encoding="utf-8"))
    packages = _dependencies(data.get("packages"))
    evidence: list[DependencyEvidence] = []
    for name, info in packages.items():
        info = info if isinstance(info, dict) else {}
        evidence.append(
            DependencyEvidence(
                ecosystem="pypi",
                name=str(info.get("name") or name),
                version=str(info.get("version", "")),
                display_name=name,
                purl=info.get("purl"),
                dependency_type=_dependency_type(info.get("dependency_type")),
                requested_by=info.get("requested_by"),
                scope=info.get("scope"),
                location_path=str(info.get("location_path") or name),
                aliases=tuple(info.get("aliases", ())),
                raw=info,
            )
        )
    return ParsedManifest(
        project=str(data.get("project", path.stem)),
        project_version=str(data.get("version", "0.0.0")),
        source_path=str(path),
        manifest_type="pip-lock-json",
        evidence=tuple(evidence),
    )


def parse_package_lock(path: Path) -> ParsedManifest:
    data = json.loads(path.read_text(encoding="utf-8"))
    root = data.get("root", {})
    root = root if isinstance(root, dict) else {}
    direct = set(_dependencies(root.get("dependencies")).keys()) | set(
        _dependencies(root.get("devDependencies")).keys()
    )
    packages = _dependencies(data.get("packages"))
    evidence: list[DependencyEvidence] = []
    for location, info in packages.items():
        if not location:
            continue
        info = info if isinstance(info, dict) else {}
        name = info.get("name")
        if not name:
            name = location.removeprefix("node_modules/")
            if "/node_modules/" in name:
                name = name.rsplit("/node_modules/", 1)[1]
        evidence.append(
            DependencyEvidence(
                ecosystem="npm",
                name=str(name),
                version=str(info.get("version", "")),
                purl=info.get("purl"),
                dependency_type=DependencyType.DIRECT if location.removeprefix("node_modules/") in direct else DependencyType.TRANSITIVE,
                requested_by=info.get("requested_by"),
                scope=info.get("dev") and "dev" or info.get("scope"),
                location_path=location,
                aliases=tuple(info.get("aliases", ())),
                raw=info,
            )
        )
    return ParsedManifest(
        project=str(data.get("project") or root.get("name") or path.stem),
        project_version=str(root.get("version") or data.get("version") or "0.0.0"),
        source_path=str(path),
        manifest_type="package-lock-json",
        evidence=tuple(evidence),
    )


def parse_maven_tree(path: Path) -> ParsedManifest:
    data = json.loads(path.read_text(encoding="utf-8"))
    evidence: list[DependencyEvidence] = []
    for info in data.get("dependencies", ()):
        if not isinstance(info, dict):
            continue
        group_id = str(info.get("group_id", ""))
        artifact_id = str(info.get("artifact_id", ""))
        evidence.append(
            DependencyEvidence(
                ecosystem="maven",
                name=f"{group_id}:{artifact_id}",
                version=str(info.get("version", "")),
                purl=info.get("purl"),
                dependency_type=_dependency_type(info.get("dependency_type")),
                requested_by=info.get("requested_by"),
                scope=info.get("scope"),
                location_path=str(info.get("location_path") or f"{group_id}:{artifact_id}"),
                aliases=tuple(info.get("aliases", ())),
                raw=info,
            )
        )
    return ParsedManifest(
        project=str(data.get("project", path.stem)),
        project_version=str(data.get("version", "0.0.0")),
        source_path=str(path),
        manifest_type="maven-tree-json",
        evidence=tuple(evidence),
    )


def parse_manifest(path: str | Path) -> ParsedManifest:
    manifest_path = Path(path)
    suffixes = {
        "pip.lock.json": parse_pip_lock,
        "package-lock.json": parse_package_lock,
        "maven-tree.json": parse_maven_tree,
    }
    name = manifest_path.name
    for suffix, parser in suffixes.items():
        if name.endswith(suffix):
            parsed = parser(manifest_path)
            return ParsedManifest(
                project=parsed.project,
                project_version=parsed.project_version,
                source_path=str(manifest_path),
                manifest_type=parsed.manifest_type,
                evidence=tuple(item.normalized() for item in parsed.evidence),
            )
    raise ManifestError(f"unsupported manifest: {manifest_path}")
