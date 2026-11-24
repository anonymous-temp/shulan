"""Artifact guards fail before receiving data, launching browsers or using models."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from utils.artifact_paths import PROJECT_ROOT, external_artifact_path


@pytest.mark.parametrize('suffix', ['', 'outputs/review.json', 'tmp/../report.html'])
def test_guard_rejects_repository_root_and_descendants(suffix):
    with pytest.raises(ValueError, match='outside the project'):
        external_artifact_path(PROJECT_ROOT / suffix)


def test_guard_resolves_relative_paths_without_creating_files(monkeypatch, tmp_path):
    monkeypatch.chdir(PROJECT_ROOT)
    with pytest.raises(ValueError, match='outside the project'):
        external_artifact_path('outputs/review.json')
    destination = tmp_path / 'external' / 'report.json'
    assert external_artifact_path(destination) == destination.resolve()
    assert not destination.parent.exists()
    monkeypatch.chdir(tmp_path)
    assert external_artifact_path('external/report.json') == destination.resolve()


def test_guard_resolves_symlink_paths_without_creating_files(tmp_path, symlink_or_skip):
    alias = tmp_path / 'checkout'
    symlink_or_skip(alias, PROJECT_ROOT, target_is_directory=True)
    with pytest.raises(ValueError, match='outside the project'):
        external_artifact_path(alias / 'missing-parent' / 'report.json')
    dangling = tmp_path / 'not-yet-created'
    symlink_or_skip(dangling, PROJECT_ROOT / 'missing-parent', target_is_directory=True)
    with pytest.raises(ValueError, match='outside the project'):
        external_artifact_path(dangling / 'report.json')
    assert not (PROJECT_ROOT / 'missing-parent').exists()


@pytest.mark.parametrize('entry', ['save', 'standalone', 'page_service'])
def test_callback_storage_rejects_repository_before_startup_or_write(entry):
    from tests.api import serve_pages
    from tests.api.scripts import report_callback_server

    target = PROJECT_ROOT / 'forbidden-callback-output'
    with pytest.raises(ValueError, match='outside the project'):
        if entry == 'save':
            report_callback_server.save_report({'count': 0, 'enabled': False}, target)
        elif entry == 'standalone':
            report_callback_server.make_app(save_dir=target)
        else:
            serve_pages.make_app(callback_save_dir=target)
    assert not target.exists()


def test_external_callback_storage_preserves_payload_and_duplicate_receipts(tmp_path):
    from tests.api.scripts.report_callback_server import save_report

    payload = {'type': 'gutBarrier', 'report': None, 'count': 0, 'enabled': False}
    folder = tmp_path / 'receipts'
    save_report(payload, folder)
    save_report(payload, folder)
    files = list(folder.glob('*.json'))
    assert len(files) == 2
    assert all(json.loads(path.read_text(encoding='utf-8')) == payload for path in files)


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['first_pass', 'document', 'http_latency', 'local_latency', 'comparison'])
async def test_benchmark_entry_rejects_repository_before_any_context_or_network_is_needed(entry):
    target = PROJECT_ROOT / 'forbidden-benchmark-output.json'
    with pytest.raises(ValueError, match='outside the project'):
        if entry == 'first_pass':
            from tests.api.scripts.analysis_first_pass_benchmark import benchmark
            await benchmark(SimpleNamespace(output=target), {})
        elif entry == 'document':
            from tests.unit._support.harnesses.qa_document_acceptance import acceptance
            await acceptance(SimpleNamespace(output=target), {})
        elif entry == 'http_latency':
            from tests.api.scripts.qa_latency import measure
            await measure(SimpleNamespace(output=target))
        elif entry == 'local_latency':
            from tests.api.scripts.local_qa_latency import run
            await run({}, target)
        else:
            from tests.api.scripts.compare_qa_models import compare
            await compare({}, target, 1)
    assert not target.exists()


@pytest.mark.parametrize('entry', ['first_pass', 'document_smoke', 'document_live'])
def test_cli_rejects_output_before_reading_service_settings(entry, monkeypatch):
    target = str(PROJECT_ROOT / 'forbidden-cli-output.json')
    if entry == 'first_pass':
        from tests.api.scripts import analysis_first_pass_benchmark as script
        settings_name = '_settings'
        argv = ['benchmark', '--live', '--baseline-service', 'not-read.py',
                '--baseline-prompt', 'not-read.txt', '--output', target]
    else:
        from tests.unit._support.harnesses import qa_document_acceptance as script
        settings_name = 'settings_from_file'
        argv = ['document', '--output', target]
        argv += (['--live', '--config', 'not-read.env', '--file-url', 'https://example.invalid/document.pdf']
                 if entry == 'document_live' else ['--smoke'])
    settings = Mock(side_effect=AssertionError('settings must not be read'))
    monkeypatch.setattr(script, settings_name, settings)
    monkeypatch.setattr('sys.argv', argv)
    with pytest.raises(SystemExit) as error:
        script.main()
    assert error.value.code == 2
    settings.assert_not_called()
    assert not Path(target).exists()


@pytest.mark.parametrize('script', [
    'tests/api/scripts/analysis_first_pass_benchmark.py',
    'tests/unit/tools/qa_document_acceptance.py',
    'tests/api/scripts/report_callback_server.py',
    'tests/api/serve_pages.py',
    'tests/api/scripts/qa_latency.py',
    'tests/api/scripts/local_qa_latency.py',
    'tests/api/scripts/compare_qa_models.py',
])
def test_direct_cli_help_works_outside_checkout_without_pythonpath(script, tmp_path):
    environment = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    result = subprocess.run([sys.executable, '-B', str(PROJECT_ROOT / script), '--help'],
                            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert 'usage:' in result.stdout
    assert not list(tmp_path.iterdir())


@pytest.fixture
def node():
    executable = shutil.which('node')
    if executable is None:
        pytest.skip('Node.js is required for the browser benchmark path guard')
    return executable


@pytest.mark.parametrize('kind', ['repository', 'relative', 'symlink', 'dangling_symlink'])
def test_browser_benchmark_rejects_unsafe_output_before_playwright_load(node, kind, tmp_path, symlink_or_skip):
    script = PROJECT_ROOT / 'tests/unit/api/stream_browser_benchmark.cjs'
    if kind == 'repository':
        target = PROJECT_ROOT / 'forbidden-browser-output'
    elif kind == 'relative':
        target = 'outputs/forbidden-browser-output'
    else:
        target = tmp_path / 'checkout'
        symlink_or_skip(target, PROJECT_ROOT if kind == 'symlink' else PROJECT_ROOT / 'missing-parent', target_is_directory=True)
        target = target / 'preview'
    result = subprocess.run([node, str(script), '--output=' + str(target)], cwd=PROJECT_ROOT,
                            text=True, encoding='utf-8', capture_output=True, timeout=10)
    assert result.returncode != 0
    assert 'artifact output must be outside the project directory' in result.stderr
    assert 'Cannot find module' not in result.stderr
    assert not (PROJECT_ROOT / 'forbidden-browser-output').exists()


@pytest.mark.parametrize('kind', ['absolute', 'external_symlink', 'omitted'])
def test_browser_benchmark_accepts_external_output_without_creating_it(node, kind, tmp_path, symlink_or_skip):
    script = PROJECT_ROOT / 'tests/unit/api/stream_browser_benchmark.cjs'
    expected = tmp_path / 'artifacts-中文' / 'preview'
    target = expected
    if kind == 'external_symlink':
        link = tmp_path / 'alias'
        symlink_or_skip(link, expected.parent, target_is_directory=True)
        target = link / 'preview'
    # Execute exactly the argument/path setup, before requiring Playwright or
    # creating an HTTP server. There are no browser, model or external requests.
    runner = """
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const file = process.argv[1], value = process.argv[2];
const prefix = fs.readFileSync(file, 'utf8').split('const {chromium} = require("playwright");')[0];
vm.runInNewContext(prefix + '\\nprocess.stdout.write(JSON.stringify(output ?? null));', {
  require, __dirname: path.dirname(file),
  process: {argv: ['node', file, ...(value ? ['--output=' + value] : [])], stdout: process.stdout}
});
"""
    result = subprocess.run([node, '-e', runner, str(script), '' if kind == 'omitted' else str(target)],
                            text=True, encoding='utf-8', capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == (None if kind == 'omitted' else str(expected))
    assert not expected.exists()
