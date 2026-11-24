"""Read-only gut-barrier generation queue release gate.

Run with ``python -m scripts.check_gut_barrier_queue --help``. No database is
imported for argument parsing or --print-version. The actual check requires a
read-only Mongo credential and prints aggregate counts only, never report data.
"""
from __future__ import annotations

import argparse
import asyncio
import json


def current_version():
    from modules.gut_barrier.pipeline import GutBarrierPipeline
    from modules.gut_barrier.policy import SERVICE_VERSION
    return SERVICE_VERSION + ":" + GutBarrierPipeline.version


def current_explanation_version():
    # Independent explanations are retired; retained records are audit-only.
    return None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-version", help="完整目标服务:临床流水线版本；默认使用本次发布代码版本")
    parser.add_argument("--expected-explanation-version", help="历史解读版本审计参数；不启用生成或投递")
    parser.add_argument("--print-version", action="store_true", help="只打印本代码版本，不连接数据库")
    args = parser.parse_args(argv)
    if args.print_version and (args.expected_version or args.expected_explanation_version):
        parser.error("--print-version and expected-version overrides cannot be used together")
    for field in ("expected_version", "expected_explanation_version"):
        value = getattr(args, field)
        if value is not None:
            value = value.strip()
            if not value or len(value) > 512 or any(ord(char) < 32 for char in value):
                parser.error(f"--{field.replace('_', '-')} must be a nonempty bounded version string")
            setattr(args, field, value)
    return args


def summarize_snapshot(rows, *, expected_version, expected_explanation_version=None):
    from modules.gut_barrier.policy import EXPLANATION_TYPE

    totals = {"queued": 0, "generating": 0, "recovering": 0}
    by_version, by_explanation = {}, {}
    pending_intents = 0
    retired_rows = []
    for row in rows:
        version, status, count = row.get("version"), row.get("status"), row.get("count")
        is_child = row.get("jobType") == EXPLANATION_TYPE
        is_intent = (not is_child and status in {"success", "partial"}
                     and row.get("explanationStatus") == "pending")
        if ((status not in totals and not is_intent)
                or isinstance(count, bool) or not isinstance(count, int) or count < 0):
            raise ValueError("invalid queue aggregate")
        explanation_version = row.get("explanationVersion") if is_child or is_intent else None
        for value in (version, explanation_version):
            if value is not None and (not isinstance(value, str) or len(value) > 512
                                      or any(ord(char) < 32 for char in value)):
                raise ValueError("invalid queue version")
        version = version or None
        oldest = row.get("oldestCreatedAt")
        if is_child or is_intent:
            # Even an explicit historical version is audit-only. Retired work
            # must never enter the main queue totals or suggest running old
            # explanation workers to drain it.
            explanation_version = explanation_version or None
            retired_rows.append({
                "generationVersion": version, "explanationVersion": explanation_version,
                "status": status, "count": count,
                "kind": "intent" if is_intent else "child",
            })
            explanation = by_explanation.setdefault((version, explanation_version), {
                "generationVersion": version, "explanationVersion": explanation_version,
                "matchesExpectedVersion": (version == expected_version and bool(expected_explanation_version)
                                           and explanation_version == expected_explanation_version),
                "queued": 0, "generating": 0, "recovering": 0,
                "pendingIntents": 0, "oldestCreatedAt": None,
            })
            explanation["pendingIntents" if is_intent else status] += count
            if is_intent:
                pending_intents += count
            if oldest and (explanation["oldestCreatedAt"] is None or oldest < explanation["oldestCreatedAt"]):
                explanation["oldestCreatedAt"] = oldest
            continue
        item = by_version.setdefault(version, {
            "version": version, "matchesExpectedVersion": version == expected_version,
            "queued": 0, "generating": 0, "recovering": 0, "oldestCreatedAt": None,
            "pendingExplanationIntents": 0,
        })
        totals[status] += count
        item[status] += count
        if oldest and (item["oldestCreatedAt"] is None or oldest < item["oldestCreatedAt"]):
            item["oldestCreatedAt"] = oldest
    versions = sorted(by_version.values(), key=lambda row: row["version"] or "")
    blocked = [row["version"] for row in versions
               if not row["matchesExpectedVersion"]
               and sum(row[key] for key in totals)]
    explanations = sorted(by_explanation.values(),
                          key=lambda row: (row["generationVersion"] or "", row["explanationVersion"] or ""))
    is_blocked = bool(blocked)
    return {"schemaVersion": "gut-barrier-queue-check.v1", "readOnly": True,
            "status": "blocked" if is_blocked else "ready", "expectedVersion": expected_version,
            "totals": totals, "versions": versions, "blockingVersions": blocked,
            "expectedExplanationVersion": expected_explanation_version,
            "pendingExplanationIntents": pending_intents, "explanationVersions": explanations,
            "blockingExplanationVersions": [],
            "retiredExplanationRows": retired_rows,
            "independentExplanationEnabled": False,
            "action": "retain_old_workers_and_drain_before_removal" if is_blocked else "no_other_version_generation_backlog",
            "snapshotOnly": True}


async def run(args, *, store=None):
    if args.print_version:
        return {"generationVersion": current_version(), "explanationVersion": current_explanation_version(),
                "databaseAccessed": False}
    expected = args.expected_version or current_version()
    expected_explanation = getattr(args, "expected_explanation_version", None) or current_explanation_version()
    client = None
    try:
        if store is None:
            # Deliberately no factory, ensure_indexes, migrations, generation,
            # callback delivery or write-concern setup on this read-only path.
            from db.mongodb import client, database
            from adapters.gut_barrier.store import MongoGutBarrierStore
            store = MongoGutBarrierStore(database["gut_barrier_report_jobs"])
        return summarize_snapshot(await store.generation_queue_snapshot(), expected_version=expected,
                                  expected_explanation_version=expected_explanation)
    finally:
        if client is not None:
            await client.close()


def main(argv=None):
    args = parse_args(argv)
    try:
        result = asyncio.run(run(args))
    except Exception as error:
        print(json.dumps({"status": "check_failed", "readOnly": True,
                          "errorType": type(error).__name__}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result.get("status") == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
