from __future__ import annotations

from collections import defaultdict

from .models import AliasDecision, Component
from .versioning import in_range


class _DSU:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _in_scope(d: AliasDecision, c: Component) -> bool:
    return (
        c.ecosystem == d.ecosystem
        and c.name in (d.name_a, d.name_b)
        and in_range(c.version, d.version_range)
        and d.matches_source(c.source)
    )


def compute_groups(components, decisions):
    """Group components by approved, in-scope alias decisions.

    Grouping is recomputed from scratch each call, so withdrawing any
    decision (including one in the middle of an indirect chain) yields
    the correct remaining groups. Components themselves are never
    mutated, so build paths survive regrouping.
    """
    dsu = _DSU()
    by_name = defaultdict(list)
    for c in components:
        by_name[c.name].append(c)

    provenance = defaultdict(set)
    approved = [d for d in decisions if d.status == "approved"]
    for d in approved:
        a_side = [c for c in by_name.get(d.name_a, []) if _in_scope(d, c)]
        b_side = [c for c in by_name.get(d.name_b, []) if _in_scope(d, c)]
        for ca in a_side:
            for cb in b_side:
                dsu.union(ca.key, cb.key)
        for c in a_side + b_side:
            provenance[dsu.find(c.key)].add(d.decision_id)

    groups = defaultdict(list)
    for c in components:
        groups[dsu.find(c.key)].append(c)

    prov = defaultdict(set)
    for root, ids in provenance.items():
        prov[dsu.find(root)] |= ids

    return [
        {"root": root, "components": members, "decisions": sorted(prov.get(root, set()))}
        for root, members in groups.items()
    ]


def detect_conflicts(components):
    """Same name+ecosystem+source observed with divergent versions."""
    by = defaultdict(set)
    for c in components:
        by[(c.name, c.ecosystem, c.source)].add(c.version)
    return [
        {"name": n, "ecosystem": e, "source": s, "versions": sorted(v)}
        for (n, e, s), v in by.items()
        if len(v) > 1
    ]


def impact_preview(components, decisions, candidate):
    """Pre-approval comparison for a candidate decision."""
    before = compute_groups(components, decisions)
    approved_candidate = AliasDecision(**{**candidate.__dict__, "status": "approved"})
    after = compute_groups(components, decisions + [approved_candidate])

    def group_of(groups):
        m = {}
        for g in groups:
            for c in g["components"]:
                m[c.key] = g["root"]
        return m

    bmap, amap = group_of(before), group_of(after)
    affected_roots = {amap[k] for k in amap if amap[k] != bmap.get(k)}
    merged_components = [c for c in components if amap[c.key] in affected_roots]

    divergent = []
    for g in after:
        if g["root"] not in affected_roots:
            continue
        by_name = defaultdict(set)
        for c in g["components"]:
            by_name[c.name].add(c.version)
        for name, versions in by_name.items():
            if len(versions) > 1:
                divergent.append({"name": name, "versions": sorted(versions)})

    in_scope_sources = {
        c.source
        for c in components
        if c.ecosystem == candidate.ecosystem
        and c.name in (candidate.name_a, candidate.name_b)
        and in_range(c.version, candidate.version_range)
        and candidate.matches_source(c.source)
    }
    unaffected_sources = sorted({c.source for c in components} - in_scope_sources)

    return {
        "decision_id": candidate.decision_id,
        "merged_components": sorted(
            {c.name + "@" + c.version + " [" + c.ecosystem + "] (" + c.source + ")" for c in merged_components}
        ),
        "version_divergence": divergent,
        "unaffected_sources": unaffected_sources,
    }
