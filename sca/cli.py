from __future__ import annotations

import argparse
import json
from pathlib import Path

from .parsers import parse_manifest
from .storage import Store


def import_files(db_path: Path, files: list[Path]) -> list[dict[str, int | str]]:
    results = []
    with Store(db_path) as store:
        for file_path in files:
            manifest = parse_manifest(file_path)
            result = store.import_manifest(manifest, file_path.read_bytes())
            result.update(
                {
                    "path": str(file_path),
                    "project": manifest.project,
                    "version": manifest.project_version,
                }
            )
            results.append(result)
    return results


def print_imports(results: list[dict[str, int | str]]) -> None:
    for result in results:
        print(
            f"{result['path']} -> {result['project']} {result['version']}: "
            f"inserted {result['inserted_records']}/{result['total_records']}"
        )


def print_report(db_path: Path, project: str, project_version: str | None, as_json: bool) -> None:
    with Store(db_path) as store:
        report = store.project_report(project, project_version)
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    project_data = report["project"]
    print(f"Project: {project_data['name']} {project_data['version']}")
    for component in report["components"]:
        versions = ", ".join(json.loads(component["versions_json"]))
        marker = "CONFLICT" if component["status"] == "conflict" else "MERGED"
        print(
            f"[{marker}] {component['purl']} ({component['display_name']}) "
            f"versions={versions} evidence={component['evidence_count']}"
        )
    print("\nEvidence:")
    for item in report["evidence"]:
        print(
            f"- {item['canonical_name']} {item['version']} "
            f"[{item['dependency_type']}] {item['source_path']} :: {item['location_path']}"
        )


def _print_preview(preview: dict) -> None:
    first, second = preview["from"], preview["to"]
    status = "ALLOWED" if preview["allowed"] else "REJECTED"
    print(f"[{status}] {first['purl']} <-> {second['purl']}")
    print(f"  scope: versions={preview['version_range']} sources={', '.join(preview['sources'])}")
    for reason in preview["reasons"]:
        print(f"  - {reason}")
    for member in preview["group_members"]:
        print(f"  member: {member['purl']} ({member['display_name']})")
    for affected in preview["affected_projects"]:
        marker = "CONFLICT" if affected["would_conflict"] else "unified"
        scope_note = "" if affected["in_scope"] else " (out of scope)"
        print(f"  project {affected['project']}: {', '.join(affected['versions'])} [{marker}]{scope_note}")
    if preview["existing_edge"]:
        print(f"  existing edge: {preview['existing_edge']['origin']} / {preview['existing_edge']['status']}")


def _print_edge(edge: dict) -> None:
    print(
        f"#{edge['id']} [{edge['status']}] ({edge['origin']}) {edge['from_purl']} <-> {edge['to_purl']} "
        f"versions={edge['version_range']} sources={', '.join(edge['sources'])}"
    )
    if edge.get("note"):
        print(f"    note: {edge['note']}")
    if edge.get("reject_reason"):
        print(f"    reject reason: {edge['reject_reason']}")


def _print_inheritance_review(review: dict) -> None:
    print(
        f"inheritance plan #{review['plan_id']}: {review['project']} "
        f"{review['from_version']} -> {review['to_version']}"
    )
    if review["finalized_at"]:
        print(f"finalized: {review['finalized_at']}")
    for item in review["items"]:
        edge = item["source_edge"]
        print(f"#{item['id']} [{item['status']}] {item['classification']}: {edge['from_purl']} <-> {edge['to_purl']}")
        for evidence in item["affected_evidence"]:
            print(
                f"  evidence: {evidence['purl']} {evidence['version']} "
                f"{evidence['source_path']} :: {evidence['location_path']}"
            )
        if item.get("note"):
            print(f"  note: {item['note']}")


def _print_joint_preview(preview: dict) -> None:
    print(f"joint preview of {len(preview['candidates'])} pending proposal(s)")
    for candidate in preview["candidates"]:
        _print_edge(candidate)
    if not preview["conflicts"]:
        print("  no combined version conflicts")
    for conflict in preview["conflicts"]:
        print(
            f"  CONFLICT in project {conflict['project']}: versions {', '.join(conflict['versions'])}"
        )
        for proposal in conflict["proposals"]:
            print(
                f"    via {proposal['role']} #{proposal['id']}: "
                f"{proposal['from_purl']} <-> {proposal['to_purl']} [{proposal['status']}]"
            )
        for evidence in conflict["evidence"]:
            print(
                f"    evidence: {evidence['canonical_name']} {evidence['version']} "
                f"{evidence['source_path']} :: {evidence['location_path']}"
            )


def _parse_edge_ids(value: str | None) -> list[int] | None:
    if value is None:
        return None
    return [int(item) for item in value.split(",") if item.strip()]


def _print_revoke_result(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(f"revoked edge #{result['revoked_edge']}; refreshed projects: {result['refreshed_projects']}")
    if result["affected_pending"]:
        print("re-checked dependent proposals still pending:")
        for edge in result["affected_pending"]:
            _print_edge(edge)
    if result["remaining_preview"]["candidates"]:
        print("re-preview of remaining proposals:")
        _print_joint_preview(result["remaining_preview"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline dependency evidence aggregation")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import", help="import one or more dependency manifests")
    import_parser.add_argument("--db", type=Path, default=Path("sca.sqlite3"))
    import_parser.add_argument("files", nargs="+", type=Path)

    report_parser = subparsers.add_parser("report", help="show project component report")
    report_parser.add_argument("--db", type=Path, default=Path("sca.sqlite3"))
    report_parser.add_argument("--project", required=True)
    report_parser.add_argument("--project-version")
    report_parser.add_argument("--json", action="store_true")

    alias_parser = subparsers.add_parser("alias", help="review, approve or revoke component aliases")
    alias_subparsers = alias_parser.add_subparsers(dest="alias_command", required=True)
    for alias_command in ("propose", "preview", "approve", "revoke"):
        command_parser = alias_subparsers.add_parser(alias_command)
        command_parser.add_argument("--db", type=Path, default=Path("sca.sqlite3"))
        command_parser.add_argument("--from", dest="from_ref")
        command_parser.add_argument("--to", dest="to_ref")
        command_parser.add_argument("--by", default="operator")
        command_parser.add_argument("--json", action="store_true")
        command_parser.add_argument("--versions", default="*", help="version range scope, e.g. '>=1.0,<2.0'")
        command_parser.add_argument("--source", action="append", default=[], help="limit scope to a project name (repeatable)")
        command_parser.add_argument("--note", help="rationale recorded with the decision")
        command_parser.add_argument("--edge-id", type=int, help="act on a stored proposal by id")
        command_parser.add_argument("--edges", help="comma-separated proposal ids for joint preview/approve")
    reject_parser = alias_subparsers.add_parser("reject", help="reject a pending proposal with a reason")
    reject_parser.add_argument("--db", type=Path, default=Path("sca.sqlite3"))
    reject_parser.add_argument("--edge-id", type=int, required=True)
    reject_parser.add_argument("--reason", required=True)
    reject_parser.add_argument("--by", default="operator")
    reject_parser.add_argument("--json", action="store_true")
    list_parser = alias_subparsers.add_parser("list")
    list_parser.add_argument("--db", type=Path, default=Path("sca.sqlite3"))
    list_parser.add_argument("--status", choices=("pending", "approved", "revoked", "rejected", "all"), default="all")
    list_parser.add_argument("--json", action="store_true")

    history_parser = subparsers.add_parser("history", help="list stored scan conclusion snapshots")
    history_parser.add_argument("--db", type=Path, default=Path("sca.sqlite3"))
    history_parser.add_argument("--project", required=True)
    history_parser.add_argument("--project-version")
    history_parser.add_argument("--snapshot-id", type=int)
    history_parser.add_argument("--json", action="store_true")

    inheritance_parser = subparsers.add_parser("inheritance", help="review alias inheritance for a new project version")
    inheritance_subparsers = inheritance_parser.add_subparsers(dest="inheritance_command", required=True)
    show_parser = inheritance_subparsers.add_parser("show")
    show_parser.add_argument("--db", type=Path, default=Path("sca.sqlite3"))
    show_parser.add_argument("--project", required=True)
    show_parser.add_argument("--project-version", required=True)
    show_parser.add_argument("--json", action="store_true")
    decide_parser = inheritance_subparsers.add_parser("decide")
    decide_parser.add_argument("--db", type=Path, default=Path("sca.sqlite3"))
    decide_parser.add_argument("--plan-id", type=int, required=True)
    decide_parser.add_argument("--items", help="comma-separated review item ids; omit means all pending")
    decide_parser.add_argument("--decision", choices=("inherit", "narrowed", "skipped"), required=True)
    decide_parser.add_argument("--versions", default="*")
    decide_parser.add_argument("--by", default="operator")
    decide_parser.add_argument("--note")
    decide_parser.add_argument("--json", action="store_true")
    reopen_parser = inheritance_subparsers.add_parser("reopen")
    reopen_parser.add_argument("--db", type=Path, default=Path("sca.sqlite3"))
    reopen_parser.add_argument("--plan-id", type=int, required=True)
    reopen_parser.add_argument("--items", required=True, help="comma-separated review item ids")
    reopen_parser.add_argument("--by", default="operator")
    reopen_parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "import":
        print_imports(import_files(args.db, args.files))
    elif args.command == "report":
        print_report(args.db, args.project, args.project_version, args.json)
    elif args.command == "alias":
        with Store(args.db) as store:
            if args.alias_command == "list":
                aliases = store.list_edges(args.status)
                if args.json:
                    print(json.dumps(aliases, ensure_ascii=False, indent=2))
                else:
                    for row in aliases:
                        _print_edge(row)
            elif args.alias_command == "reject":
                edge = store.reject_edge(args.edge_id, args.reason, decided_by=args.by)
                if args.json:
                    print(json.dumps(edge, ensure_ascii=False, indent=2))
                else:
                    _print_edge(edge)
            else:
                edge_ids = _parse_edge_ids(args.edges)
                sources = args.source or None
                if args.alias_command == "preview" and (edge_ids is not None or not args.from_ref):
                    preview = store.joint_preview(edge_ids)
                    if args.json:
                        print(json.dumps(preview, ensure_ascii=False, indent=2))
                    else:
                        _print_joint_preview(preview)
                elif args.alias_command == "approve" and edge_ids is not None:
                    result = store.approve_edges(edge_ids, decided_by=args.by, note=args.note)
                    if args.json:
                        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
                    else:
                        for edge in result["approved"]:
                            _print_edge(edge)
                        print(f"approved; refreshed projects: {result['refreshed_projects']}")
                        if result["remaining_pending"]:
                            print("re-preview of remaining proposals:")
                            _print_joint_preview(result["remaining_preview"])
                elif args.alias_command == "revoke" and args.edge_id is not None:
                    _print_revoke_result(store.revoke_edge(args.edge_id, decided_by=args.by), args.json)
                else:
                    if not args.from_ref or not args.to_ref:
                        raise SystemExit("--from and --to are required for this alias operation")
                    if args.alias_command == "preview":
                        preview = store.alias_preview(
                            args.from_ref, args.to_ref, version_range=args.versions, sources=sources
                        )
                        if args.json:
                            print(json.dumps(preview, ensure_ascii=False, indent=2))
                        else:
                            _print_preview(preview)
                    elif args.alias_command == "propose":
                        result = store.propose_alias(
                            args.from_ref, args.to_ref, created_by=args.by,
                            version_range=args.versions, sources=sources, note=args.note,
                        )
                        if args.json:
                            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
                        else:
                            print(f"proposed edge #{result['edge_id']} [{result['status']}]")
                            _print_preview(result["preview"])
                    elif args.alias_command == "approve":
                        result = store.approve_alias(
                            args.from_ref, args.to_ref, decided_by=args.by,
                            version_range=args.versions, sources=sources, note=args.note,
                        )
                        if args.json:
                            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
                        else:
                            _print_preview(result["preview"])
                            print(f"approved; refreshed projects: {result['refreshed_projects']}")
                    elif args.alias_command == "revoke":
                        result = store.revoke_alias(args.from_ref, args.to_ref, decided_by=args.by)
                        _print_revoke_result(result, args.json)
    elif args.command == "history":
        with Store(args.db) as store:
            history = store.project_history(args.project, args.project_version)
            if args.snapshot_id is not None:
                history = [entry for entry in history if entry["snapshot_id"] == args.snapshot_id]
            if args.json:
                print(json.dumps(history, ensure_ascii=False, indent=2))
            else:
                for entry in history:
                    components = entry["report"]["components"]
                    conflicts = sum(1 for component in components if component["status"] == "conflict")
                    print(
                        f"#{entry['snapshot_id']} {entry['taken_at']} ({entry['reason']}) "
                        f"components={len(components)} conflicts={conflicts}"
                    )
    elif args.command == "inheritance":
        with Store(args.db) as store:
            if args.inheritance_command == "show":
                review = store.inheritance_review(args.project, args.project_version)
                if review is None:
                    raise SystemExit("no inheritance review found")
                if args.json:
                    print(json.dumps(review, ensure_ascii=False, indent=2))
                else:
                    _print_inheritance_review(review)
            elif args.inheritance_command == "decide":
                item_ids = _parse_edge_ids(args.items)
                result = store.decide_inheritance(
                    args.plan_id,
                    item_ids,
                    args.decision,
                    version_range=args.versions,
                    decided_by=args.by,
                    note=args.note,
                )
                if args.json:
                    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
                else:
                    _print_inheritance_review(result["review"])
            elif args.inheritance_command == "reopen":
                result = store.reopen_inheritance_items(
                    args.plan_id, _parse_edge_ids(args.items), decided_by=args.by
                )
                if args.json:
                    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
                else:
                    _print_inheritance_review(result["review"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
