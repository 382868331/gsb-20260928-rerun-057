from __future__ import annotations

import re


def _parse(version: str) -> tuple:
    parts = re.split(r"[.\-+]", version.strip())
    out = []
    for p in parts:
        if p.isdigit():
            out.append((1, int(p), ""))
        else:
            out.append((0, 0, p))
    return tuple(out)


def compare(a: str, b: str) -> int:
    ta, tb = _parse(a), _parse(b)
    n = max(len(ta), len(tb))
    pad = (1, 0, "")
    ta += (pad,) * (n - len(ta))
    tb += (pad,) * (n - len(tb))
    return (ta > tb) - (ta < tb)


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
        m = _CLAUSE.match(clause)
        if not m:
            raise ValueError(f"bad version clause: {clause!r}")
        op, ver = m.group(1) or "==", m.group(2)
        c = compare(version, ver)
        ok = {
            "==": c == 0,
            "!=": c != 0,
            ">=": c >= 0,
            "<=": c <= 0,
            ">": c > 0,
            "<": c < 0,
        }[op]
        if not ok:
            return False
    return True
