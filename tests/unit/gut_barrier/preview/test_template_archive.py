"""Only the current release is bundled; historical previews require an archive."""
import builtins
from copy import deepcopy
import json
import re

import pytest

from adapters.gut_barrier import frontend_template
from adapters.gut_barrier.frontend_template import load_static_template
from adapters.gut_barrier.rendering import render_report_html
from modules.gut_barrier.display_versions import LATEST_DISPLAY_TEMPLATE
from modules.gut_barrier.reference_order import publish_ordered_references
from modules.gut_barrier.reference_pins import pinned_references
from scripts.render_gut_barrier_callback import render_callback
from tests.unit.gut_barrier.preview import archive_support


HISTORICAL = [f"gut-barrier-static.v{number}" for number in range(1, 11)]


@pytest.fixture
def external_archive(tmp_path):
    directory = tmp_path / "archive"
    directory.mkdir()
    return directory


def block_configuration_import(monkeypatch):
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "config" or name.startswith("config."):
            raise AssertionError("explicit archives and latest templates must not load configuration")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)


def write_fixture(directory, version="gut-barrier-static.v1"):
    # Synthetic loader fixture, not an archived clinical release or its prose.
    asset = {
        "templateVersion": version,
        "reportSchemaVersion": "gut-barrier-public.v2",
        "sourceVersions": {"testFixture": "not-clinical-history"},
        "sectionOrder": ["overview", "fixture"],
        "overviewInsertions": [], "overviewSlots": [], "appendixBlocks": [],
        "fixedSections": [{"id": "fixture", "title": "Synthetic fixture section",
                           "subtitle": None, "visible": True, "blocks": [
                               {"kind": "paragraph", "title": None,
                                "text": "Synthetic archive loader fixture, not clinical history."}]}],
    }
    path = directory / f"{version}.json"
    path.write_text(json.dumps(asset, ensure_ascii=False), encoding="utf-8")
    return asset, path


def public_report():
    return {"schemaVersion": "gut-barrier-public.v2",
            "meta": {"title": "Preview fixture", "reportId": "archive-fixture"},
            "sections": [{"id": "overview", "title": "结果", "subtitle": None,
                          "visible": True, "blocks": [{"kind": "paragraph", "title": None,
                          "text": "Archive fixture, not a patient report."}]}]}


@pytest.mark.parametrize("version", HISTORICAL)
def test_missing_archive_never_silently_substitutes_latest(version, monkeypatch):
    block_configuration_import(monkeypatch)
    with pytest.raises(FileNotFoundError, match="is archived") as error:
        load_static_template(version)
    assert "--template-archive-dir" in str(error.value)
    with pytest.raises(FileNotFoundError, match="is archived"):
        render_report_html(public_report(), template_version=version)


def test_unbound_receipt_still_requires_its_historical_default(monkeypatch, tmp_path):
    block_configuration_import(monkeypatch)
    report = public_report()
    original = deepcopy(report)
    report["meta"]["displayTemplateVersion"] = LATEST_DISPLAY_TEMPLATE
    with pytest.raises(FileNotFoundError, match="gut-barrier-static.v1"):
        render_callback({"type": "gutBarrier", "status": "success", "report": report}, tmp_path / "output")
    assert not (tmp_path / "output").exists()
    report["meta"].pop("displayTemplateVersion")
    assert report == original


@pytest.mark.parametrize("archive_directory", [None, "relative-archive", "/nonexistent/template-archive"])
def test_bundled_template_is_independent_of_archive_directory(archive_directory, monkeypatch):
    version = LATEST_DISPLAY_TEMPLATE
    block_configuration_import(monkeypatch)
    asset = load_static_template(version, archive_directory=archive_directory)
    assert asset["templateVersion"] == version
    report = public_report()
    report["sections"].append({"id": "appendix", "title": "附录", "subtitle": None,
                               "visible": True, "blocks": []})
    # A current receipt freezes its bibliography before the renderer consumes it.
    publish_ordered_references(report, deepcopy(report), asset, reference_limit=15,
                               pinned_references=pinned_references(), allow_fixed_budget_omission=True)
    before = deepcopy(report)
    rendered = render_report_html(report, template_version=version,
                                  template_archive_dir=archive_directory)
    data = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', rendered, re.S)[1]
    assert json.loads(data) == report == before


def test_v13_binds_fixed_sources_with_its_reference_policy_without_changing_callback(monkeypatch):
    from adapters.gut_barrier.frontend_template import expand_dynamic_report
    calls = []

    def bind(asset, report, *, allow_omitted_fixed):
        calls.append((asset["templateVersion"], allow_omitted_fixed))
        return asset

    monkeypatch.setattr("modules.gut_barrier.reference_order.bind_ordered_static_copy", bind)
    report = public_report()
    before = deepcopy(report)
    expand_dynamic_report(report, template_version=LATEST_DISPLAY_TEMPLATE)
    assert calls == [(LATEST_DISPLAY_TEMPLATE, True)]
    assert report == before


@pytest.mark.parametrize("version", HISTORICAL)
def test_explicit_external_archive_loads_only_requested_version(external_archive, version):
    asset, _ = write_fixture(external_archive, version)
    assert load_static_template(version, archive_directory=external_archive) == asset
    with pytest.raises(FileNotFoundError, match="is archived"):
        load_static_template(version)


def test_explicit_archive_does_not_load_configuration(external_archive, monkeypatch):
    asset, _ = write_fixture(external_archive)
    block_configuration_import(monkeypatch)
    assert load_static_template(archive_directory=external_archive) == asset


def test_absent_archived_version_does_not_use_a_different_release(external_archive):
    write_fixture(external_archive, "gut-barrier-static.v2")
    with pytest.raises(FileNotFoundError, match="gut-barrier-static.v1 not found"):
        load_static_template(archive_directory=external_archive)


@pytest.mark.parametrize("field,value", [("templateVersion", LATEST_DISPLAY_TEMPLATE),
    ("reportSchemaVersion", "gut-barrier-public.future")])
def test_archive_must_match_requested_release_and_wire_schema(external_archive, field, value):
    asset, path = write_fixture(external_archive)
    asset[field] = value
    path.write_text(json.dumps(asset), encoding="utf-8")
    with pytest.raises(ValueError, match="version mismatch"):
        load_static_template(archive_directory=external_archive)


@pytest.mark.parametrize("archive", ["relative/archive", str(frontend_template._PROJECT_ROOT),
    str(frontend_template.STATIC_TEMPLATE_DIRECTORY)])
def test_archive_location_must_be_absolute_and_outside_project(archive):
    with pytest.raises(ValueError, match="absolute|outside"):
        load_static_template(archive_directory=archive)


def test_symlinked_archive_cannot_point_back_into_project(external_archive, tmp_path, symlink_or_skip):
    symlink = tmp_path / "project-alias"
    symlink_or_skip(symlink, frontend_template.STATIC_TEMPLATE_DIRECTORY, target_is_directory=True)
    with pytest.raises(ValueError, match="outside"):
        load_static_template(archive_directory=symlink)


def test_template_symlink_cannot_escape_archive(external_archive, tmp_path, symlink_or_skip):
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    symlink_or_skip(external_archive / "gut-barrier-static.v1.json", outside)
    with pytest.raises(ValueError, match="inside its external archive"):
        load_static_template(archive_directory=external_archive)


def test_callback_renderer_passes_archive_without_changing_frozen_json(external_archive, tmp_path):
    write_fixture(external_archive)
    callback = {"type": "gutBarrier", "status": "success", "report": public_report()}
    before = deepcopy(callback)
    target = render_callback(callback, tmp_path / "preview", template_archive_dir=external_archive)
    assert target.is_file()
    assert json.loads((target.parent / "report.json").read_text(encoding="utf-8")) == callback["report"]
    context = json.loads((target.parent / "render-context.json").read_text(encoding="utf-8"))
    assert context["templateVersion"] == "gut-barrier-static.v1"
    assert callback == before


def test_explicit_historical_fixture_binds_synthetic_archive_to_real_renderer(
    external_archive, monkeypatch, request
):
    asset, path = write_fixture(external_archive)
    asset["fixedSections"][0]["title"] = "Synthetic archive fixture section"
    path.write_text(json.dumps(asset), encoding="utf-8")
    monkeypatch.setattr(archive_support, "HISTORICAL_ARCHIVE_DIR", external_archive)
    assert archive_support.historical_archive() == external_archive
    # Reading the helper is side-effect free; only the explicit fixture binds rendering.
    with pytest.raises(FileNotFoundError, match="is archived"):
        load_static_template()
    assert request.getfixturevalue("historical_templates") == external_archive
    rendered = render_report_html(public_report(), template_version="gut-barrier-static.v1")
    assert "Synthetic archive fixture section" in rendered
    with pytest.raises(ValueError, match="outside"):
        load_static_template(archive_directory=frontend_template._PROJECT_ROOT)


def test_historical_regression_without_explicit_path_skips_without_binding_an_archive(monkeypatch):
    monkeypatch.setattr(archive_support, "HISTORICAL_ARCHIVE_DIR", None)
    with pytest.raises(pytest.skip.Exception, match="HISTORICAL_ARCHIVE_DIR"):
        archive_support.historical_archive()
    with pytest.raises(FileNotFoundError, match="is archived"):
        load_static_template()
