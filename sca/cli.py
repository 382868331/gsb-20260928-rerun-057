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


def run_alias_command(args: argparse.Namespace) -> None:
    with Store(args.db) as store:
        if args.alias_command == "propose":
            alias_id = store.propose_alias(
                args.ecosystem,
                args.name,
                args.target_name,
                target_ecosystem=args.target_ecosystem,
                note=args.note,
            )
            print(f"proposed alias #{alias_id} (pending approval)")
        elif args.alias_command == "list":
            for alias in store.list_aliases():
                print(
                    f"#{alias['id']} [{alias['status']}] {alias['ecosystem']}: "
                    f"{alias['canonical_name']} -> {alias['target_canonical_name']}"
                )
        elif args.alias_command == "preview":
            print(json.dumps(store.preview_alias(args.alias_id), ensure_ascii=False, indent=2))
        elif args.alias_command == "approve":
            preview = store.approve_alias(args.alias_id)
            print(f"approved alias #{args.alias_id}")
            print(json.dumps(preview["impact"], ensure_ascii=False, indent=2))
        elif args.alias_command == "revoke":
            store.revoke_alias(args.alias_id)
            print(f"revoked alias #{args.alias_id}; original components and evidence restored")
        elif args.alias_command == "history":
            for event in store.alias_history(args.alias_id):
                print(f"#{event['id']} alias={event['alias_id']} {event['action']} at {event['created_at']}")


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

    alias_parser = subparsers.add_parser("alias", help="manage manual component aliases")
    alias_subparsers = alias_parser.add_subparsers(dest="alias_command", required=True)

    propose_parser = alias_subparsers.add_parser("propose", help="propose a manual alias (pending approval)")
    propose_parser.add_argument("--db", type=Path, default=Path("sca.sqlite3"))
    propose_parser.add_argument("--ecosystem", required=True)
    propose_parser.add_argument("--name", required=True)
    propose_parser.add_argument("--target-name", required=True)
    propose_parser.add_argument("--target-ecosystem")
    propose_parser.add_argument("--note")

    list_parser = alias_subparsers.add_parser("list", help="list manual aliases")
    list_parser.add_argument("--db", type=Path, default=Path("sca.sqlite3"))

    for action in ("preview", "approve", "revoke"):
        action_parser = alias_subparsers.add_parser(action, help=f"{action} a manual alias")
        action_parser.add_argument("--db", type=Path, default=Path("sca.sqlite3"))
        action_parser.add_argument("--alias-id", type=int, required=True)

    history_parser = alias_subparsers.add_parser("history", help="show alias decision history")
    history_parser.add_argument("--db", type=Path, default=Path("sca.sqlite3"))
    history_parser.add_argument("--alias-id", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "import":
        print_imports(import_files(args.db, args.files))
    elif args.command == "report":
        print_report(args.db, args.project, args.project_version, args.json)
    elif args.command == "alias":
        run_alias_command(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
