from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path
from urllib.parse import quote, unquote

from .models import ComponentCoordinate, DependencyRecord, ParsedManifest


def canonicalize_pypi_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def parse_requirement(text: str) -> tuple[str, str | None, str | None]:
    expression = text.split("#", 1)[0].strip()
    match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(.*)$", expression)
    if not match:
        raise ValueError(f"invalid requirement: {text}")
    name = match.group(1)
    spec = match.group(2).strip().lstrip("(").rstrip(")").strip() or None
    exact = None
    if spec:
        exact_match = re.search(r"(?:^|,)s*(?:===|==)s*([A-Za-z0-9][A-Za-z0-9._+!-]*)", spec)
        if exact_match:
            exact = exact_match.group(1)
    return name, exact, spec


def parse_purl(purl: str) -> tuple[str, str, str]:
    if not purl.startswith("pkg:"):
        raise ValueError(f"invalid package URL: {purl}")
    body = purl[4:]
    body = body.split("#", 1)[0].split("?", 1)[0]
    if "/" in body:
        ecosystem, name = body.split("/", 1)
    else:
        ecosystem, name = "pypi", body
    name = unquote(name.split("@", 1)[0])
    if ecosystem.lower() == "pypi":
        name = canonicalize_pypi_name(name)
    return ecosystem.lower(), name, purl


def coordinate_from_json(name: str, data: dict[str, object]) -> ComponentCoordinate:
    purl = data.get("purl")
    if isinstance(purl, str) and purl:
        ecosystem, canonical_name, normalized_purl = parse_purl(purl)
        return ComponentCoordinate(ecosystem, canonical_name, name, normalized_purl)
    ecosystem = str(data.get("ecosystem", "pypi")).lower()
    explicit_name = data.get("canonical_name")
    canonical_name = canonicalize_pypi_name(str(explicit_name)) if explicit_name else canonicalize_pypi_name(name)
    normalized_purl = f"pkg:{ecosystem}/{quote(canonical_name)}" if ecosystem != "pypi" else f"pkg:pypi/{quote(canonical_name)}"
    return ComponentCoordinate(ecosystem, canonical_name, name, normalized_purl)


def parse_manifest(path: str | Path, default_artifact: str | None = None) -> ParsedManifest:
    return parse_manifest_with_project(path, None, None, default_artifact)


def parse_manifest_with_project(
    path: str | Path,
    project_name: str | None = None,
    project_version: str | None = None,
    default_artifact: str | None = None,
) -> ParsedManifest:
    manifest_path = Path(path)
    content = manifest_path.read_bytes()
    content_hash = hashlib.sha256(content).hexdigest()
    suffix = manifest_path.suffix.lower()
    if suffix == ".json":
        return parse_lock_json(manifest_path, content, content_hash, project_name, project_version, default_artifact)
    if suffix in {".toml", ".txt"}:
        return parse_pyproject_or_requirements(
            manifest_path, content, content_hash, project_name, project_version, default_artifact
        )
    raise ValueError(f"unsupported manifest format: {manifest_path}")


def parse_pyproject_or_requirements(
    path: Path,
    content: bytes,
    content_hash: str,
    override_project_name: str | None,
    override_project_version: str | None,
    default_artifact: str | None,
) -> ParsedManifest:
    if path.suffix.lower() == ".toml":
        document = tomllib.loads(content.decode())
        project = document.get("project", {})
        project_name = str(project.get("name") or override_project_name or path.parent.name)
        project_version = str(project.get("version") or override_project_version or "0.0.0")
        requirements = project.get("dependencies", [])
        manifest_format = "pyproject"
        artifact = default_artifact or "build:metadata"
    else:
        detected_name = override_project_name or path.parent.name
        detected_version = override_project_version or "0.0.0"
        requirements = []
        for line_text in content.decode().splitlines():
            stripped = line_text.strip()
            if not stripped:
                continue
            metadata = re.match(r"^#\s*project\s*:\s*([^@\s]+)(?:@(.+))?$", stripped)
            if metadata:
                detected_name = metadata.group(1)
                if metadata.group(2):
                    detected_version = metadata.group(2)
                continue
            if stripped.startswith("#"):
                continue
            requirements.append(line_text)
        project_name = detected_name
        project_version = detected_version
        manifest_format = "requirements"
        artifact = default_artifact or "requirements:default"

    records = []
    for index, requirement_text in enumerate(requirements):
        name, version, spec = parse_requirement(str(requirement_text))
        coordinate = ComponentCoordinate("pypi", canonicalize_pypi_name(name), name, f"pkg:pypi/{quote(canonicalize_pypi_name(name))}")
        records.append(
            DependencyRecord(
                source_path=str(path),
                index=index,
                display_name=name,
                coordinate=coordinate,
                version=version,
                version_spec=spec,
                dependency_type="direct",
                artifact_path=artifact,
                raw={"requirement": requirement_text},
            )
        )
    return ParsedManifest(str(path), manifest_format, content_hash, project_name, project_version, tuple(records))


def parse_lock_json(
    path: Path,
    content: bytes,
    content_hash: str,
    override_project_name: str | None,
    override_project_version: str | None,
    default_artifact: str | None,
) -> ParsedManifest:
    document = json.loads(content.decode())
    project_name = str(document.get("project") or override_project_name or path.parent.name)
    project_version = str(document.get("version") or override_project_version or "0.0.0")
    records: list[DependencyRecord] = []
    artifacts = document.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise ValueError("lock JSON 'artifacts' must be a list")
    for artifact in artifacts:
        artifact_path = str(artifact.get("path", default_artifact or "unknown"))
        dependencies = artifact.get("dependencies", [])
        if not isinstance(dependencies, list):
            raise ValueError("artifact dependencies must be a list")
        for index, item in enumerate(dependencies):
            if not isinstance(item, dict):
                raise ValueError("dependency entries must be objects")
            name = str(item["name"])
            coordinate = coordinate_from_json(name, item)
            chain_value = item.get("dependency_chain", ())
            chain = tuple(str(value) for value in chain_value) if isinstance(chain_value, list) else ()
            requires_value = item.get("requires", ())
            requires = tuple(str(value) for value in requires_value) if isinstance(requires_value, list) else ()
            records.append(
                DependencyRecord(
                    source_path=str(path),
                    index=len(records),
                    display_name=name,
                    coordinate=coordinate,
                    version=str(item["version"]) if item.get("version") is not None else None,
                    version_spec=str(item["spec"]) if item.get("spec") is not None else None,
                    dependency_type=str(item.get("type", "direct")),
                    artifact_path=artifact_path,
                    dependency_chain=chain,
                    requires=requires,
                    raw=item,
                )
            )
    return ParsedManifest(str(path), "lock-json", content_hash, project_name, project_version, tuple(records))
