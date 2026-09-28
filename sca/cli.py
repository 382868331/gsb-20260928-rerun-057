from __future__ import annotations

import argparse
import json
from pathlib import Path

from .store import connect, evidence_for, import_manifest, report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="offline-sca")
    parser.add_argument("--db", default="sca-results.db")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("manifests", nargs="+")
    import_parser.add_argument("--artifact")
    import_parser.add_argument("--project-name")
    import_parser.add_argument("--project-version")

    subparsers.add_parser("report")

    evidence_parser = subparsers.add_parser("evidence")
    evidence_parser.add_argument("ecosystem")
    evidence_parser.add_argument("canonical_name")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with connect(args.db) as connection:
        if args.command == "import":
            for manifest in args.manifests:
                inserted, manifest_id = import_manifest(
                    connection,
                    Path(manifest),
                    args.artifact,
                    args.project_name,
                    args.project_version,
                )
                action = "imported" if inserted else "unchanged"
                print(f"{action}: {manifest} (#{manifest_id})")
        elif args.command == "report":
            rows = report(connection)
            print("PROJECT\tECOSYSTEM\tCOMPONENT\tSTATUS\tVERSION\tALIASES\tARTIFACTS")
            for row in rows:
                version_value = row["selected_version"] or row["versions"]
                print(
                    f"{row['project_name']}@{row['project_version']}\t{row['ecosystem']}\t"
                    f"{row['canonical_name']}\t{row['status']}\t{version_value}\t"
                    f"{row['aliases']}\t{row['artifact_paths']}"
                )
        elif args.command == "evidence":
            rows = [dict(row) for row in evidence_for(connection, args.ecosystem, args.canonical_name)]
            print(json.dumps(rows, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
