from __future__ import annotations

import argparse
import json
import sys

from .engine import compute_groups, detect_conflicts, impact_preview
from .store import Store


def _store(args):
    return Store(args.db)


def cmd_scan(args):
    st = _store(args)
    with open(args.file, encoding="utf-8") as fh:
        components = json.load(fh)
    added = st.record_scan(args.scan_id, args.source, components)
    known = {(c.name, c.ecosystem, c.source) for c in st.conflicts()}
    for cf in detect_conflicts(st.components()):
        if (cf["name"], cf["ecosystem"], cf["source"]) not in known:
            st.record_conflict(cf["name"], cf["ecosystem"], cf["source"], cf["versions"])
    print("scan %s: %d new component(s)" % (args.scan_id, added))


def cmd_propose(args):
    st = _store(args)
    d = st.propose_alias(args.name_a, args.name_b, args.ecosystem, args.versions, args.source)
    print(json.dumps(impact_preview(st.components(), st.decisions(), d), indent=2, ensure_ascii=False))


def cmd_preview(args):
    st = _store(args)
    d = _find_decision(st, args.decision_id)
    print(json.dumps(impact_preview(st.components(), st.decisions(), d), indent=2, ensure_ascii=False))


def cmd_approve(args):
    st = _store(args)
    st.set_decision_status(args.decision_id, "approved")
    print(args.decision_id + " approved")


def cmd_withdraw(args):
    st = _store(args)
    st.set_decision_status(args.decision_id, "withdrawn")
    print(args.decision_id + " withdrawn; groups re-derived")


def cmd_groups(args):
    st = _store(args)
    out = []
    for g in compute_groups(st.components(), st.decisions()):
        out.append(
            {
                "components": [
                    {
                        "name": c.name,
                        "version": c.version,
                        "ecosystem": c.ecosystem,
                        "source": c.source,
                        "build_path": c.build_path,
                        "scan_id": c.scan_id,
                    }
                    for c in g["components"]
                ],
                "decisions": g["decisions"],
            }
        )
    print(json.dumps(out, indent=2, ensure_ascii=False))


def cmd_conflicts(args):
    st = _store(args)
    rows = []
    for c in st.conflicts():
        d = c.__dict__.copy()
        d["versions"] = list(c.versions)
        rows.append(d)
    print(json.dumps(rows, indent=2, ensure_ascii=False))


def cmd_decisions(args):
    st = _store(args)
    rows = []
    for d in st.decisions():
        x = d.__dict__.copy()
        x["sources"] = list(d.sources)
        rows.append(x)
    print(json.dumps(rows, indent=2, ensure_ascii=False))


def cmd_history(args):
    st = _store(args)
    print(json.dumps(st.events, indent=2, ensure_ascii=False))


def _find_decision(st, decision_id):
    for d in st.decisions():
        if d.decision_id == decision_id:
            return d
    raise SystemExit("unknown decision: " + decision_id)


def main(argv=None):
    p = argparse.ArgumentParser(prog="sca", description="Scoped manual aliasing for SCA")
    p.add_argument("--db", default="sca-events.jsonl")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan")
    s.add_argument("scan_id")
    s.add_argument("source")
    s.add_argument("file")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("propose")
    s.add_argument("name_a")
    s.add_argument("name_b")
    s.add_argument("--ecosystem", required=True)
    s.add_argument("--versions", default="*")
    s.add_argument("--source", action="append", default=[])
    s.set_defaults(fn=cmd_propose)

    s = sub.add_parser("preview")
    s.add_argument("decision_id")
    s.set_defaults(fn=cmd_preview)

    s = sub.add_parser("approve")
    s.add_argument("decision_id")
    s.set_defaults(fn=cmd_approve)

    s = sub.add_parser("withdraw")
    s.add_argument("decision_id")
    s.set_defaults(fn=cmd_withdraw)

    for name, fn in [("groups", cmd_groups), ("conflicts", cmd_conflicts),
                     ("decisions", cmd_decisions), ("history", cmd_history)]:
        s = sub.add_parser(name)
        s.set_defaults(fn=fn)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main(sys.argv[1:])
