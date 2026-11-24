from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.gut_barrier.store import MongoGutBarrierStore
from scripts import check_gut_barrier_queue as check


def group(version, status="queued", count=1, oldest="2026-09-24T00:00:00+00:00", **details):
    return {"version": version, "status": status, "count": count, "oldestCreatedAt": oldest, **details}


def test_only_current_generation_is_ready_but_not_declared_empty_or_fully_delivered():
    report = check.summarize_snapshot([
        group("new", "queued", 21), group("new", "generating", 20), group("new", "recovering", 1),
    ], expected_version="new")
    assert report["status"] == "ready" and report["blockingVersions"] == []
    assert report["totals"] == {"queued": 21, "generating": 20, "recovering": 1}
    assert report["readOnly"] is True and report["snapshotOnly"] is True
    assert "delivered" not in report and "request" not in report


@pytest.mark.parametrize("status", ["queued", "generating", "recovering"])
@pytest.mark.parametrize("version", ["old", None, ""])
def test_old_or_unknown_version_unfinished_work_blocks_removal(status, version):
    report = check.summarize_snapshot([group(version, status)], expected_version="new")
    assert report["status"] == "blocked"
    assert report["blockingVersions"] == [version or None]
    assert report["action"] == "retain_old_workers_and_drain_before_removal"


def test_empty_read_only_snapshot_does_not_invent_backlog():
    report = check.summarize_snapshot([], expected_version="new")
    assert report["status"] == "ready" and report["versions"] == []
    assert not any(report["totals"].values())


@pytest.mark.parametrize("invalid", [
    {**group("new"), "status": "success"}, {**group("new"), "count": True},
    {**group("new"), "count": -1}, {**group("new"), "count": "1"},
    group({"patient": "must not be reflected"}), group("bad\nversion"),
])
def test_malformed_queue_aggregate_is_a_failure_not_a_ready_release(invalid):
    with pytest.raises(ValueError):
        check.summarize_snapshot([invalid], expected_version="new")


@pytest.mark.asyncio
async def test_store_preflight_aggregates_only_status_version_counts_without_index_or_writes():
    cursor = SimpleNamespace(to_list=AsyncMock(return_value=[{
        "_id": {"version": "old", "status": "queued"}, "count": 2,
        "oldestCreatedAt": datetime(2026, 9, 24),
    }]))
    collection = SimpleNamespace(aggregate=AsyncMock(return_value=cursor))
    store = MongoGutBarrierStore(collection)
    store.ensure_indexes = AsyncMock(side_effect=AssertionError("read-only checks cannot create indexes"))
    rows = await store.generation_queue_snapshot()
    assert rows == [group("old", count=2)]
    stages = collection.aggregate.await_args.args[0]
    assert stages[0] == {"$match": {"$or": [
        {"status": {"$in": ["queued", "generating", "recovering"]}},
        {"status": {"$in": ["success", "partial"]}, "explanationStatus": "pending"},
    ]}}
    assert set(stages[1]["$group"]) == {"_id", "count", "oldestCreatedAt"}
    assert set(stages[1]["$group"]["_id"]) == {
        "version", "status", "jobType", "explanationVersion", "explanationStatus"}
    assert collection.aggregate.await_args.kwargs == {"maxTimeMS": 10000}
    cursor.to_list.assert_awaited_once_with(length=1001)
    store.ensure_indexes.assert_not_awaited()
    assert "request" not in str(stages) and "body" not in str(stages) and "userId" not in str(stages)


@pytest.mark.asyncio
async def test_store_preflight_does_not_silently_truncate_excess_version_groups():
    cursor = SimpleNamespace(to_list=AsyncMock(return_value=[{}] * 1001))
    store = MongoGutBarrierStore(SimpleNamespace(aggregate=AsyncMock(return_value=cursor)))
    with pytest.raises(ValueError, match="too many"):
        await store.generation_queue_snapshot()


@pytest.mark.asyncio
async def test_run_with_injected_store_never_accesses_database_or_invokes_generation():
    store = SimpleNamespace(generation_queue_snapshot=AsyncMock(return_value=[group("old")]))
    result = await check.run(check.parse_args([
        "--expected-version", "new", "--expected-explanation-version", "explain-new"]), store=store)
    assert result["status"] == "blocked"
    store.generation_queue_snapshot.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_print_version_does_not_even_inspect_store(monkeypatch):
    monkeypatch.setattr(check, "current_version", lambda: "current")
    monkeypatch.setattr(check, "current_explanation_version", lambda: "current-explain")
    store = SimpleNamespace(generation_queue_snapshot=AsyncMock(side_effect=AssertionError("no database")))
    result = await check.run(check.parse_args(["--print-version"]), store=store)
    assert result == {"generationVersion": "current", "explanationVersion": "current-explain",
                      "databaseAccessed": False}
    store.generation_queue_snapshot.assert_not_awaited()


@pytest.mark.parametrize("status,expected_code", [("ready", 0), ("blocked", 2)])
def test_cli_exit_code_is_an_automation_release_gate(monkeypatch, capsys, status, expected_code):
    monkeypatch.setattr(check, "run", AsyncMock(return_value={"status": status, "readOnly": True}))
    assert check.main(["--expected-version", "new"]) == expected_code
    assert json.loads(capsys.readouterr().out)["status"] == status


def test_cli_database_outage_is_nonzero_and_does_not_leak_credentials(monkeypatch, capsys):
    monkeypatch.setattr(check, "run", AsyncMock(side_effect=ConnectionError("mongodb://private-secret")))
    assert check.main(["--expected-version", "new"]) == 1
    output = capsys.readouterr().out
    assert "private-secret" not in output and "ConnectionError" in output


@pytest.mark.parametrize("arguments", [
    ["--expected-version", ""], ["--expected-version", "new\nversion"],
    ["--expected-version", "x" * 513], ["--print-version", "--expected-version", "new"],
    ["--expected-explanation-version", ""], ["--expected-explanation-version", "old\nexplain"],
    ["--expected-explanation-version", "x" * 513],
    ["--print-version", "--expected-explanation-version", "new"],
    ["--migrate"], ["--delete"],
])
def test_cli_rejects_bad_arguments_and_exposes_no_write_mode(arguments):
    with pytest.raises(SystemExit) as error:
        check.parse_args(arguments)
    assert error.value.code == 2


@pytest.mark.parametrize("status", ["queued", "generating", "recovering"])
@pytest.mark.parametrize("explanation_version", ["explain-old", None, ""])
def test_explicit_explanation_version_only_audits_retired_children(status, explanation_version):
    report = check.summarize_snapshot([
        group("current", status, 2, jobType="gutBarrierExplain", explanationVersion=explanation_version),
    ], expected_version="current", expected_explanation_version="explain-current")
    assert report["status"] == "ready" and report["blockingVersions"] == []
    assert report["totals"][status] == 0 and report["pendingExplanationIntents"] == 0
    assert report["blockingExplanationVersions"] == []
    details = report["explanationVersions"]
    assert len(details) == 1 and details[0]["generationVersion"] == "current"
    assert details[0]["explanationVersion"] == (explanation_version or None)
    assert details[0][status] == 2
    assert report["retiredExplanationRows"][0]["count"] == 2
    assert report["action"] == "no_other_version_generation_backlog"


@pytest.mark.parametrize("status", ["success", "partial"])
def test_old_unmaterialized_intent_is_audit_only_with_zero_active_generation(status):
    report = check.summarize_snapshot([
        group("current", status, 3, explanationStatus="pending", explanationVersion="explain-old"),
    ], expected_version="current", expected_explanation_version="explain-current")
    assert report["status"] == "ready" and not any(report["totals"].values())
    assert report["pendingExplanationIntents"] == 3
    assert report["versions"] == report["blockingExplanationVersions"] == []
    assert report["explanationVersions"][0]["pendingIntents"] == 3


def test_retired_intent_from_old_main_version_is_visible_but_never_blocks():
    report = check.summarize_snapshot([
        group("old", "success", explanationStatus="pending", explanationVersion="explain-current"),
    ], expected_version="current", expected_explanation_version="explain-current")
    assert report["status"] == "ready" and report["blockingVersions"] == []
    assert report["blockingExplanationVersions"] == []
    assert report["retiredExplanationRows"][0]["generationVersion"] == "old"


def test_current_main_child_and_intents_are_separate_counts_and_keep_old_output_keys():
    report = check.summarize_snapshot([
        group("current", "queued", 2),
        group("current", "generating", 4, jobType="gutBarrierExplain", explanationVersion="explain-current"),
        group("current", "success", 3, explanationStatus="pending", explanationVersion="explain-current"),
    ], expected_version="current", expected_explanation_version="explain-current")
    assert report["status"] == "ready"
    assert report["totals"] == {"queued": 2, "generating": 0, "recovering": 0}
    assert report["pendingExplanationIntents"] == 3
    assert report["expectedExplanationVersion"] == "explain-current"
    assert report["blockingVersions"] == report["blockingExplanationVersions"] == []
    assert len(report["versions"]) == 1 and len(report["explanationVersions"]) == 1
    detail = report["explanationVersions"][0]
    assert detail["queued"] == 0 and detail["generating"] == 4 and detail["pendingIntents"] == 3
    assert {"schemaVersion", "readOnly", "status", "expectedVersion", "totals", "versions",
            "blockingVersions", "action", "snapshotOnly"} <= set(report)


def test_retired_explanation_queue_does_not_block_main_report_release():
    report = check.summarize_snapshot([
        group("current", jobType="gutBarrierExplain", explanationVersion="explain-current"),
    ], expected_version="current")
    assert report["status"] == "ready"
    assert report["independentExplanationEnabled"] is False
    assert report["totals"] == {"queued": 0, "generating": 0, "recovering": 0}
    assert report["retiredExplanationRows"] == [{
        "generationVersion": "current", "explanationVersion": "explain-current",
        "status": "queued", "count": 1, "kind": "child",
    }]


@pytest.mark.parametrize("explanation_version", [False, [], {"private": "not-output"}, "x" * 513, "bad\nversion"])
def test_malformed_explanation_versions_fail_instead_of_being_printed(explanation_version):
    with pytest.raises(ValueError):
        check.summarize_snapshot([
            group("current", jobType="gutBarrierExplain", explanationVersion=explanation_version),
        ], expected_version="current", expected_explanation_version="explain-current")


@pytest.mark.asyncio
async def test_snapshot_preserves_legacy_row_shape_and_adds_only_explanation_discriminators():
    rows = [
        {"_id": {"version": "current", "status": "queued"}, "count": 2},
        {"_id": {"version": "current", "status": "queued", "jobType": "gutBarrierExplain",
                 "explanationVersion": "explain-old"}, "count": 3},
        {"_id": {"version": "current", "status": "success", "explanationStatus": "pending"}, "count": 4},
    ]
    cursor = SimpleNamespace(to_list=AsyncMock(return_value=rows))
    store = MongoGutBarrierStore(SimpleNamespace(aggregate=AsyncMock(return_value=cursor)))
    result = await store.generation_queue_snapshot()
    assert result[0] == group("current", count=2, oldest=None)
    assert result[1] == group("current", count=3, oldest=None,
                              jobType="gutBarrierExplain", explanationVersion="explain-old")
    assert result[2] == group("current", "success", 4, oldest=None,
                              explanationStatus="pending", explanationVersion=None)


@pytest.mark.asyncio
async def test_run_never_enables_explanations_even_with_explicit_audit_version(monkeypatch):
    monkeypatch.setattr(check, "current_version", lambda: "current")
    store = SimpleNamespace(generation_queue_snapshot=AsyncMock(return_value=[
        group("current", jobType="gutBarrierExplain", explanationVersion="explain-old"),
    ]))
    result = await check.run(check.parse_args([
        "--expected-explanation-version", "explain-current"]), store=store)
    assert result["status"] == "ready" and result["expectedExplanationVersion"] == "explain-current"
    assert result["independentExplanationEnabled"] is False
    assert not any(result["totals"].values())


def _clean_subprocess_env(root, environ):
    # Isolate application configuration, not the OS/activated interpreter.
    # Windows needs SystemRoot; Conda may need its activated DLL search paths.
    runtime_keys = {
        "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR",
        "CONDA_PREFIX", "CONDA_DLL_SEARCH_MODIFICATION_ENABLE",
    }
    result = {key.upper(): value for key, value in environ.items() if key.upper() in runtime_keys}
    result.setdefault("PATH", os.defpath)
    result.update(PYTHONPATH=str(root), PYTHONIOENCODING="utf-8")
    return result


@pytest.mark.parametrize("key_case", ["upper", "lower", "mixed"])
def test_clean_subprocess_env_preserves_windows_runtime_not_application_settings(key_case, tmp_path):
    runtime = {
        "Path": r"C:\Miniconda3\envs\shulan;C:\Miniconda3\envs\shulan\Library\bin;C:\Windows\System32",
        "SystemRoot": r"C:\Windows",
        "WinDir": r"C:\Windows",
        "ComSpec": r"C:\Windows\System32\cmd.exe",
        "PathExt": ".COM;.EXE;.BAT;.CMD",
        "Temp": r"C:\Users\tester\AppData\Local\Temp",
        "Tmp": r"C:\Users\tester\AppData\Local\Temp",
        "CONDA_PREFIX": r"C:\Miniconda3\envs\shulan",
        "CONDA_DLL_SEARCH_MODIFICATION_ENABLE": "1",
    }
    environ = {(getattr(key, key_case)() if key_case != "mixed" else key): value
               for key, value in runtime.items()}
    environ.update({
        "SHULAN_MONGODB_URI": "must-not-inherit",
        "SHULAN_LLM_API_KEY": "must-not-inherit",
        "MONGODB_URI": "must-not-inherit",
        "PYTHONPATH": "must-not-inherit",
        "PYTHONHOME": "must-not-inherit",
        "PYTEST_CURRENT_TEST": "must-not-inherit",
    })
    result = _clean_subprocess_env(tmp_path, environ)
    assert result == {**{key.upper(): value for key, value in runtime.items()},
                      "PYTHONPATH": str(tmp_path), "PYTHONIOENCODING": "utf-8"}
    assert environ["PYTHONPATH"] == "must-not-inherit"


def test_clean_subprocess_env_has_portable_defaults_without_guessing_windows_paths(tmp_path):
    assert _clean_subprocess_env(tmp_path, {}) == {
        "PATH": os.defpath, "PYTHONPATH": str(tmp_path), "PYTHONIOENCODING": "utf-8",
    }


def test_print_version_works_in_clean_subprocess_without_loading_environment_or_database(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("SHULAN_PREFLIGHT_TEST_SENTINEL", "must-not-inherit")
    monkeypatch.setenv("MONGODB_URI", "must-not-inherit")
    command = (
        "import os, sys; "
        "assert not any(key.startswith('SHULAN_') for key in os.environ), 'application environment leaked'; "
        "assert 'MONGODB_URI' not in os.environ, 'database environment leaked'; "
        "assert 'PYTEST_CURRENT_TEST' not in os.environ, 'pytest environment leaked'; "
        "from scripts.check_gut_barrier_queue import main; "
        "code = main(['--print-version']); "
        "assert 'config.env' not in sys.modules, 'config.env was imported'; "
        "assert 'db.mongodb' not in sys.modules, 'db.mongodb was imported'; "
        "assert 'modules.gut_barrier.explanation' not in sys.modules, 'legacy explanation was imported'; "
        "raise SystemExit(code)"
    )
    process = subprocess.run([sys.executable, "-B", "-c", command], cwd=tmp_path,
                             env=_clean_subprocess_env(root, os.environ),
                             text=True, encoding="utf-8", capture_output=True, timeout=10)
    assert process.returncode == 0, (
        f"exit code: {process.returncode}\nstdout:\n{process.stdout}\nstderr:\n{process.stderr}"
    )
    result = json.loads(process.stdout)
    assert result == {"generationVersion": check.current_version(),
                      "explanationVersion": None, "databaseAccessed": False}
