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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "import":
        print_imports(import_files(args.db, args.files))
    elif args.command == "report":
        print_report(args.db, args.project, args.project_version, args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

