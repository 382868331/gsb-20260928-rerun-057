# Scoped Manual Aliasing for Software Composition Analysis

> Note: this directory is the original standalone prototype (JSONL event
> log, runnable only from inside sca/). The scoped-alias capability is now
> integrated into the main package at the repository root — same CLI entry
> point and SQLite database as import / report / history, plus joint
> preview of pending proposals, partial adoption with re-preview, and
> withdrawal re-checks. See the root README and tests/test_scoped_decisions.py.

Manual alias decisions are constrained to an explicit **ecosystem**,
**version range**, and **source scope**. Confirming that two names are
equivalent in one project never merges them in other projects or in
incompatible versions, and recorded conflict evidence stays visible.

## Model

- Component: immutable scan observation (name, ecosystem, version,
  source, build path, scan id).
- AliasDecision: links two names within ecosystem + version range +
  source scope; lifecycle pending -> approved -> withdrawn.
- ConflictEvidence: version divergence records, kept after aliasing.
- Everything is an append-only event log (sca-events.jsonl); state is
  re-derived on every read, so save/reopen and re-imports are safe.

## CLI

    python -m sca.cli --db events.jsonl scan scan-1 proj-a components.json
    python -m sca.cli --db events.jsonl propose lodash lodash-es +        --ecosystem npm --versions ">=4.0.0,<5.0.0" --source proj-a
    python -m sca.cli --db events.jsonl preview  dec-<id>
    python -m sca.cli --db events.jsonl approve  dec-<id>
    python -m sca.cli --db events.jsonl withdraw dec-<id>
    python -m sca.cli --db events.jsonl groups | conflicts | decisions | history

## Guarantees

- Scoped merging: decisions apply only inside their ecosystem /
  version range / source scope (engine._in_scope).
- Impact preview: propose/preview shows components that would merge,
  records still diverging in version, and unaffected sources.
- Re-derivable grouping: groups are recomputed from approved
  decisions, so withdrawing a middle link of an indirect chain restores
  correct groups; components (and their build paths) are never mutated.
- Idempotent ingestion: duplicate scan observations are skipped;
  incremental scans only add new components.
- Traceability: every mutation is a timestamped event; merged groups
  carry the ids of the decisions that produced them.

## Tests

    cd sca && python -m unittest discover tests -v
