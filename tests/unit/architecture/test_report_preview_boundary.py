"""Keep optional local report previews out of the production dependency graph."""
from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import pytest

from adapters.gut_barrier.frontend_template import load_static_template
from adapters.gut_barrier.render_context import callback_render_context, render_context_headers
from modules.gut_barrier.display_versions import (
    LATEST_DISPLAY_TEMPLATE, CLOSED_BOUNDARY_DISPLAY_TEMPLATE, SUPPORTED_DISPLAY_TEMPLATES,
)
from modules.gut_barrier.reference_order import load_reference_asset


PROJECT_ROOT = Path(__file__).resolve().parents[3]
PREVIEW_MODULES = (
    "shulan_report_preview", "adapters.report.dev_preview",
    "adapters.gut_barrier.frontend_template", "adapters.gut_barrier.graphics",
    "adapters.gut_barrier.latest_view", "adapters.gut_barrier.rendering",
)


def _is_preview_module(name):
    return any(name == prefix or name.startswith(prefix + ".") for prefix in PREVIEW_MODULES)


def test_backend_has_no_dependency_on_local_html_preview_package():
    for directory in ("adapters", "modules", "bootstrap", "web", "runtime"):
        for path in (PROJECT_ROOT / directory).rglob("*.py"):
            module = ".".join(path.relative_to(PROJECT_ROOT).with_suffix("").parts)
            # Preview source is version-controlled, but the API must not import
            # it. Preview modules may compose one another in the local tool.
            if _is_preview_module(module):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    imported = node.module or ""
                    if node.level:
                        imported = importlib.util.resolve_name(
                            "." * node.level + imported, module.rsplit(".", 1)[0])
                    modules = [imported, *(f"{imported}.{alias.name}" for alias in node.names)]
                assert not any(_is_preview_module(name) for name in modules), path


def test_only_current_preview_template_is_retained_as_a_source_asset():
    assets = PROJECT_ROOT / "adapters/gut_barrier/assets"
    versions = {LATEST_DISPLAY_TEMPLATE, CLOSED_BOUNDARY_DISPLAY_TEMPLATE}
    assert {path.stem for path in assets.glob("*.json")} == versions
    for version in versions:
        template = json.loads((assets / f"{version}.json").read_text(encoding="utf-8"))
        assert template["templateVersion"] == version
        assert template["reportSchemaVersion"] == "gut-barrier-public.v2"
        assert not {"userId", "sessionId", "subject", "assessment"}.intersection(template)


@pytest.mark.parametrize("version", ["gut-barrier-static.v10"])
@pytest.mark.parametrize("loader", [load_static_template, load_reference_asset])
def test_previous_templates_require_their_exact_explicit_archive(version, loader, tmp_path):
    with pytest.raises(FileNotFoundError, match="is archived"):
        loader(version)
    # Minimal loader fixture, not a historical report or a latest-template copy.
    archived = {"templateVersion": version, "reportSchemaVersion": "gut-barrier-public.v2", "fixture": "first"}
    path = tmp_path / f"{version}.json"
    path.write_text(json.dumps(archived), encoding="utf-8")
    assert loader(version, archive_directory=tmp_path) == archived
    archived["fixture"] = "second"
    path.write_text(json.dumps(archived), encoding="utf-8")
    assert loader(version, archive_directory=tmp_path) == archived
    archived["templateVersion"] = LATEST_DISPLAY_TEMPLATE
    path.write_text(json.dumps(archived), encoding="utf-8")
    with pytest.raises(ValueError, match="version mismatch"):
        loader(version, archive_directory=tmp_path)


def test_historical_callback_bindings_need_no_static_files():
    callback = {"type": "gutBarrier", "status": "success", "finalReportId": "synthetic",
                "report": {"meta": {"reportId": "synthetic"}, "sections": []}}
    assert SUPPORTED_DISPLAY_TEMPLATES == {f"gut-barrier-static.v{n}" for n in range(1, 15)}
    for version in SUPPORTED_DISPLAY_TEMPLATES:
        context = callback_render_context(callback, version)
        headers = render_context_headers(callback, context)
        assert headers["X-Gut-Barrier-Template-Version"] == version
        assert headers["X-Gut-Barrier-Report-Id"] == "synthetic"
