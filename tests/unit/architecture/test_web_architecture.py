"""Static and construction gates for the modular Tornado boundary."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from bootstrap.container import AppContainer
from runtime import ApplicationLifecycle
from runtime import TaskSupervisor
from web.routes import build_routes

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _tree(relative_path: str) -> ast.AST:
    path = PROJECT_ROOT / relative_path
    return ast.parse(path.read_text(encoding='utf-8'), filename=str(path))


def _imports(tree: ast.AST) -> set[str]:
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(f'{"." * node.level}{node.module}')
    return result


def _container() -> AppContainer:
    lifecycle = ApplicationLifecycle()
    lifecycle.mark_ready()
    dependency = SimpleNamespace()
    return AppContainer(
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


def test_route_registry_is_complete_named_and_unique() -> None:
    routes = build_routes(_container())
    names = [route.name for route in routes]
    patterns = [route.regex.pattern for route in routes]
    assert len(routes) == 32
    assert all(names)
    assert len(names) == len(set(names))
    assert len(patterns) == len(set(patterns))
    assert 'report.submit' in names
    assert 'gut-barrier.submit' in names
    assert 'health-analysis.generate' in names
    assert {'questionnaire.collection', 'questionnaire.submit', 'questionnaire.factor-assessment'} <= set(names)


def test_non_report_business_routes_use_explicit_dependencies() -> None:
    for route in build_routes(_container()):
        if route.name.startswith('health') and route.name in {
            'health',
            'health.live',
            'health.ready',
        }:
            continue
        if route.name == 'report.submit':
            assert set(route.kwargs or {}) == {
                'pipeline',
                'task_coordinator',
                'task_supervisor',
            }
            continue
        if route.name == 'gut-barrier.submit':
            assert set(route.kwargs or {}) == {'service', 'task_supervisor'}
            continue
        if route.name == 'health-analysis.generate':
            assert set(route.kwargs or {}) == {'service'}
            continue
        assert route.kwargs
        assert set(route.kwargs) <= {'service', 'processing_service_factory'}


def test_server_is_only_a_compatibility_facade() -> None:
    tree = _tree('server.py')
    handler_classes = [
        node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name.endswith('Handler')
    ]
    assert handler_classes == []
    text = (PROJECT_ROOT / 'server.py').read_text(encoding='utf-8')
    assert '/api/v1/' not in text


def test_non_report_web_handlers_do_not_import_infrastructure() -> None:
    forbidden = ('adapters', 'db', 'aion', 'agents.graphs', 'health_profile')
    for path in (PROJECT_ROOT / 'web' / 'handlers').glob('*.py'):
        imports = _imports(ast.parse(path.read_text(encoding='utf-8')))
        unexpected = sorted(
            name for name in imports if any(name == item or name.startswith(f'{item}.') for item in forbidden)
        )
        assert unexpected == [], f'{path.name}: {unexpected}'


def test_non_report_handlers_do_not_use_business_service_locator() -> None:
    forbidden_keys = {
        'agent_runtime',
        'qa_manager',
        'chat_manager',
        'health_profile_service',
        'health_report_source',
        'report_pipeline',
    }
    for path in (PROJECT_ROOT / 'web' / 'handlers').glob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        referenced = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in forbidden_keys
        }
        assert referenced == set(), f'{path.name}: {sorted(referenced)}'


def test_application_facades_are_tornado_independent() -> None:
    for path in (PROJECT_ROOT / 'modules').glob('*/application/**/*.py'):
        imports = _imports(ast.parse(path.read_text(encoding='utf-8')))
        assert not any(name == 'tornado' or name.startswith('tornado.') for name in imports), str(
            path.relative_to(PROJECT_ROOT)
        )


def test_application_services_depend_only_on_owned_ports() -> None:
    forbidden = (
        'adapters',
        'aion',
        'config.env',
        'db',
        'health_profile',
    )
    for path in (PROJECT_ROOT / 'modules').glob('*/application/**/*.py'):
        imports = _imports(ast.parse(path.read_text(encoding='utf-8')))
        unexpected = sorted(
            name for name in imports if any(name == item or name.startswith(f'{item}.') for item in forbidden)
        )
        assert unexpected == [], f'{path.relative_to(PROJECT_ROOT)}: {unexpected}'
