"""Architecture guards for the report API migration."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from bootstrap.container import AppContainer
from runtime import ApplicationLifecycle, TaskSupervisor
from web.handlers.report import ReportGenerateHandler
from web.routes import build_routes

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _registered_routes():
    lifecycle = ApplicationLifecycle()
    lifecycle.mark_ready()
    dependency = SimpleNamespace()
    container = AppContainer(
        agent_runtime=dependency,
        analysis_service_factory=lambda: dependency,
        record_service=dependency,
        chat_service=dependency,
        health_profile_service=dependency,
        health_report_source=dependency,
        record_profile_source=dependency,
        chat_profile_source=dependency,
        health_profile_memory_provider=dependency,
        health_profile_ingestion_worker=dependency,
        report_pipeline=dependency,
        report_task_coordinator=dependency,
        lifecycle=lifecycle,
        task_supervisor=TaskSupervisor(),
    )
    return build_routes(container)


def test_all_public_routes_have_one_authoritative_registry():
    routes = list(_registered_routes())
    patterns = [route.regex.pattern for route in routes]

    assert len(patterns) == len(set(patterns))
    report_routes = [route for route in routes if route.name == "report.submit"]
    assert len(report_routes) == 1
    assert report_routes[0].regex.pattern == r"/api/v1/report$"
    assert report_routes[0].handler_class is ReportGenerateHandler
    assert set(report_routes[0].kwargs or {}) == {
        "pipeline",
        "task_coordinator",
        "task_supervisor",
    }


def test_legacy_report_api_package_is_removed():
    assert not (PROJECT_ROOT / "report_api").exists()


def test_production_code_does_not_import_legacy_report_api_package():
    roots = [
        PROJECT_ROOT / "server.py",
        PROJECT_ROOT / "web",
        PROJECT_ROOT / "modules",
        PROJECT_ROOT / "adapters",
    ]
    violations: list[str] = []
    for path in _python_files(roots):
        for imported in _imported_modules(path):
            if imported == "report_api" or imported.startswith("report_api."):
                violations.append(f"{path.relative_to(PROJECT_ROOT)}: {imported}")

    assert violations == []


def test_report_business_module_has_no_transport_or_adapter_dependencies():
    forbidden_roots = {
        "web",
        "adapters",
        "tornado",
        "httpx",
        "aiohttp",
        "aion",
        "loongspears",
    }
    violations: list[str] = []
    for path in (PROJECT_ROOT / "modules" / "report").rglob("*.py"):
        for imported in _imported_modules(path):
            if imported.split(".", 1)[0] in forbidden_roots:
                violations.append(f"{path.relative_to(PROJECT_ROOT)}: {imported}")

    assert violations == []


def _python_files(roots: list[Path]):
    for root in roots:
        if root.is_file():
            yield root
        else:
            yield from root.rglob("*.py")


def _imported_modules(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
