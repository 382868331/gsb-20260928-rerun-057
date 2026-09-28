from __future__ import annotations

import re
from urllib.parse import quote

from .models import DependencyEvidence

_PYTHON_NAME_RE = re.compile(r"[-_.]+")


def normalize_python_name(name: str) -> str:
    return _PYTHON_NAME_RE.sub("-", name.strip().lower())


def normalize_npm_name(name: str) -> str:
    value = name.strip().lower()
    if value.startswith("@") and "/" in value:
        scope, package = value[1:].split("/", 1)
        return f"@{scope}/{package}"
    return value


def component_key(ecosystem: str, name: str) -> tuple[str, str]:
    ecosystem_value = ecosystem.strip().lower()
    if ecosystem_value == "pypi":
        return ecosystem_value, normalize_python_name(name)
    if ecosystem_value == "npm":
        return ecosystem_value, normalize_npm_name(name)
    if ecosystem_value == "maven":
        group_id, artifact_id = name.split(":", 1)
        return ecosystem_value, f"{group_id.strip().lower()}:{artifact_id.strip().lower()}"
    return ecosystem_value, name.strip().lower()


def versionless_purl(ecosystem: str, name: str) -> str:
    ecosystem_value, canonical_name = component_key(ecosystem, name)
    if ecosystem_value == "maven":
        group_id, artifact_id = canonical_name.split(":", 1)
        return f"pkg:maven/{group_id}/{artifact_id}"
    encoded = quote(canonical_name, safe="@/")
    return f"pkg:{ecosystem_value}/{encoded}"


def identify(item: DependencyEvidence) -> tuple[str, str, str]:
    ecosystem, canonical_name = component_key(item.ecosystem, item.name)
    generated = versionless_purl(ecosystem, item.name)
    purl = strip_version_from_purl(item.purl) if item.purl and item.purl.startswith(f"pkg:{ecosystem}/") else generated
    return ecosystem, canonical_name, purl


def strip_version_from_purl(purl: str) -> str:
    purl = purl.split("?", 1)[0].split("#", 1)[0]
    name_part = purl.rsplit("/", 1)[-1]
    at_position = name_part.rfind("@")
    if at_position > 0:
        purl = purl[: -(len(name_part) - at_position)]
    return purl
