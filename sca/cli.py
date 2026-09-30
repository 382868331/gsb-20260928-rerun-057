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
    for reason in preview["reasons"]:
        print(f"  - {reason}")
    for member in preview["group_members"]:
        print(f"  member: {member['purl']} ({member['display_name']})")
    for affected in preview["affected_projects"]:
        marker = "CONFLICT" if affected["would_conflict"] else "unified"
        print(f"  project {affected['project_id']}: {', '.join(affected['versions'])} [{marker}]")
    if preview["existing_edge"]:
        print(f"  existing edge: {preview['existing_edge']['origin']} / {preview['existing_edge']['status']}")


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
        command_parser.add_argument("--from", dest="from_ref", required=True)
        command_parser.add_argument("--to", dest="to_ref", required=True)
        command_parser.add_argument("--by", default="operator")
        command_parser.add_argument("--json", action="store_true")
    list_parser = alias_subparsers.add_parser("list")
    list_parser.add_argument("--db", type=Path, default=Path("sca.sqlite3"))
    list_parser.add_argument("--status", choices=("pending", "approved", "revoked", "all"), default="all")
    list_parser.add_argument("--json", action="store_true")

    history_parser = subparsers.add_parser("history", help="list stored scan conclusion snapshots")
    history_parser.add_argument("--db", type=Path, default=Path("sca.sqlite3"))
    history_parser.add_argument("--project", required=True)
    history_parser.add_argument("--project-version")
    history_parser.add_argument("--snapshot-id", type=int)
    history_parser.add_argument("--json", action="store_true")
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
                aliases = store.pending_aliases() if args.status == "pending" else store.conn.execute(
                    """
                    SELECT e.id, e.origin, e.status, e.created_at, e.decided_at,
                           c1.purl AS from_purl, c2.purl AS to_purl
                    FROM alias_edges e
                    JOIN components c1 ON c1.id=e.from_component_id
                    JOIN components c2 ON c2.id=e.to_component_id
                    ORDER BY e.id
                    """
                ).fetchall()
                aliases = [dict(row) for row in aliases]
                if args.status != "all":
                    aliases = [row for row in aliases if row["status"] == args.status]
                if args.json:
                    print(json.dumps(aliases, ensure_ascii=False, indent=2))
                else:
                    for row in aliases:
                        print(f"#{row['id']} [{row['status']}] ({row['origin']}) {row['from_purl']} <-> {row['to_purl']}")
            elif args.alias_command == "preview":
                preview = store.alias_preview(args.from_ref, args.to_ref)
                if args.json:
                    print(json.dumps(preview, ensure_ascii=False, indent=2))
                else:
                    _print_preview(preview)
            elif args.alias_command == "propose":
                result = store.propose_alias(args.from_ref, args.to_ref, created_by=args.by)
                if args.json:
                    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
                else:
                    print(f"proposed edge #{result['edge_id']} [{result['status']}]")
                    _print_preview(result["preview"])
            elif args.alias_command == "approve":
                result = store.approve_alias(args.from_ref, args.to_ref, decided_by=args.by)
                if args.json:
                    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
                else:
                    _print_preview(result["preview"])
                    print(f"approved; refreshed projects: {result['refreshed_projects']}")
            elif args.alias_command == "revoke":
                result = store.revoke_alias(args.from_ref, args.to_ref, decided_by=args.by)
                if args.json:
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    print(f"revoked edge #{result['revoked_edge']}; refreshed projects: {result['refreshed_projects']}")
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
