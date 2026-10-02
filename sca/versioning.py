from __future__ import annotations

import re


def _parse(version: str) -> tuple:
    parts = re.split(r"[.\-+]", version.strip())
    out = []
    for part in parts:
        if part.isdigit():
            out.append((1, int(part), ""))
        else:
            out.append((0, 0, part))
    return tuple(out)


def compare_versions(first: str, second: str) -> int:
    first_parts, second_parts = _parse(first), _parse(second)
    size = max(len(first_parts), len(second_parts))
    pad = (1, 0, "")
    first_parts += (pad,) * (size - len(first_parts))
    second_parts += (pad,) * (size - len(second_parts))
    return (first_parts > second_parts) - (first_parts < second_parts)


_CLAUSE = re.compile(r"^(>=|<=|==|!=|>|<)?\s*(\S+)$")


def in_range(version: str, spec: str) -> bool:
    """Check a version against a comma-separated range like '>=1.0,<2.0'."""
    spec = (spec or "*").strip()
    if spec in ("*", ""):
        return True
    for clause in spec.split(","):
        clause = clause.strip()
        if not clause:
            continue
        match = _CLAUSE.match(clause)
        if not match:
            raise ValueError(f"bad version clause: {clause!r}")
        op, target = match.group(1) or "==", match.group(2)
        result = compare_versions(version, target)
        ok = {
            "==": result == 0,
            "!=": result != 0,
            ">=": result >= 0,
            "<=": result <= 0,
            ">": result > 0,
            "<": result < 0,
        }[op]
        if not ok:
            return False
    return True
