"""V10 freezes one first-citation namespace before callback delivery."""
from copy import deepcopy
import json
from pathlib import Path
import re

import pytest

from adapters.gut_barrier.frontend_template import expand_dynamic_report, project_display_report
from adapters.gut_barrier.rendering import render_report_html
from modules.gut_barrier.display_versions import (
    ORDERED_REFERENCE_DISPLAY_TEMPLATE, LATEST_DISPLAY_TEMPLATE,
)
from modules.gut_barrier.publication import project_callback_report
from modules.gut_barrier.reference_order import (
    PRIMARY_TITLE, SUPPLEMENT_TITLE, REFERENCE_FIELDS, bind_ordered_static_copy,
    load_reference_asset, publish_ordered_references, reference_keys,
)
from tests.unit.gut_barrier.test_product_display_feedback import _source


def ref(title, url=None, **fields):
    return {**dict.fromkeys(REFERENCE_FIELDS), "citation": title, "url": url, **fields}


def paragraph(text, title=None):
    return {"kind": "paragraph", "title": title, "text": text}


def section(identifier, blocks):
    return {"id": identifier, "title": identifier, "subtitle": None, "visible": True, "blocks": blocks}


def fixture():
    report = {"schemaVersion": "gut-barrier-public.v2", "meta": {"title": "test", "reportId": "ordered"}, "sections": [
        section("overview", [paragraph("Patient first [2]. Repeat [2]. Table 1; year [2026].", "anchor")]),
        section("disease_risks", [paragraph("Later source [3].")]),
        section("appendix", [{"kind": "references", "title": PRIMARY_TITLE, "items": [
            ref("Uncited selected guidance", "https://example.test/unused"),
            ref("Patient finding", "https://example.test/patient"),
            ref("Shared publication", "https://example.test/shared", doi="10.1234/shared"),
        ]}]),
    ]}
    asset = {"templateVersion": ORDERED_REFERENCE_DISPLAY_TEMPLATE, "reportSchemaVersion": "gut-barrier-public.v2",
        "sectionOrder": ["overview", "disease_risks", "repair_plan", "appendix"], "overviewInsertions": [],
        "fixedSections": [section("repair_plan", [paragraph("Fixed cited [补1]. Repeated shared [1].")])],
        "appendixBlocks": [
            {"kind": "references", "title": PRIMARY_TITLE, "items": [
                ref("Same publication via DOI", "https://doi.org/10.1234/SHARED", year=2024)]},
            {"kind": "references", "title": "固定补充依据", "items": [
                ref("Fixed first", "https://example.test/fixed"),
                ref("Uncited fixed source", "https://example.test/uncited-fixed")]}],
    }
    return report, deepcopy(report), asset


def references(report, title):
    return next(block["items"] for s in report["sections"] for block in s["blocks"] if block.get("title") == title)


def test_final_reading_order_deduplicates_and_retains_uncited_sources_only_internally():
    report, source, asset = fixture()
    original_source, original_asset = deepcopy(source), deepcopy(asset)
    audit = publish_ordered_references(report, source, asset)
    assert report["sections"][0]["blocks"][0]["text"] == "Patient first [1]. Repeat [1]. Table 1; year [2026]."
    assert report["sections"][1]["blocks"][0]["text"] == "Later source [2]."
    assert [r["citation"] for r in references(report, PRIMARY_TITLE)] == ["Patient finding", "Shared publication", "Fixed first"]
    assert references(report, PRIMARY_TITLE)[1]["year"] == 2024
    assert {row["reference"]["citation"] for row in audit if row["bodyNumber"] is None} == {
        "Uncited selected guidance", "Uncited fixed source"}
    assert not any(b["title"] == SUPPLEMENT_TITLE for s in report["sections"] for b in s["blocks"])
    bound = bind_ordered_static_copy(asset, report)
    assert bound["fixedSections"][0]["blocks"][0]["text"] == "Fixed cited [3]. Repeated shared [2]."
    assert len(audit) == 9 and any(row["bodyNumber"] is None for row in audit)
    assert source == original_source and asset == original_asset
    assert all(set(r) == set(REFERENCE_FIELDS) for r in references(report, PRIMARY_TITLE))


def test_fixed_overview_insertion_is_numbered_before_later_dynamic_body():
    report, source, asset = fixture()
    asset["overviewInsertions"] = [{"anchor": {"kind": "paragraph", "title": "anchor"}, "position": "before",
                                    "block": paragraph("Fixed first [补1].")}]
    publish_ordered_references(report, source, asset)
    assert report["sections"][0]["blocks"][0]["text"].startswith("Patient first [2].")
    assert [r["citation"] for r in references(report, PRIMARY_TITLE)] == ["Fixed first", "Patient finding", "Shared publication"]
    assert bind_ordered_static_copy(asset, report)["overviewInsertions"][0]["block"]["text"] == "Fixed first [1]."


def test_hidden_metadata_does_not_turn_an_uncited_source_into_body_reference():
    report, source, asset = fixture()
    report["sections"][0]["blocks"].append({"kind": "facts", "title": "metadata", "items": [{"label": "source", "value": "[1]"}]})
    report["sections"].append({**section("hidden", [paragraph("Hidden [1].")]), "visible": False})
    audit = publish_ordered_references(report, source, asset)
    assert any(row["reference"]["citation"] == "Uncited selected guidance" and row["bodyNumber"] is None for row in audit)
    assert "Uncited selected guidance" not in [r["citation"] for r in references(report, PRIMARY_TITLE)]


def test_unused_fixed_insertion_stays_internal_and_does_not_require_body_number():
    report, source, asset = fixture()
    asset["overviewInsertions"] = [{"anchor": {"kind": "paragraph", "title": "absent"}, "position": "before",
                                    "block": paragraph("Not rendered [补2].")}]
    audit = publish_ordered_references(report, source, asset)
    assert any(row["reference"]["citation"] == "Uncited fixed source" and row["bodyNumber"] is None for row in audit)
    assert "Uncited fixed source" not in [r["citation"] for r in references(report, PRIMARY_TITLE)]
    bind_ordered_static_copy(asset, report)


@pytest.mark.parametrize("left,right", [
    (ref("A", doi="10.1234/ABC"), ref("B", "https://doi.org/10.1234/abc")),
    (ref("A", "https://example.test/a/"), ref("B", "https://example.test/a#section")),
    (ref("A", "https://www.who.int/publications/i/item/9789240015128"),
     ref("B", "https://iris.who.int/bitstream/handle/10665/336656/9789240015128-eng.pdf")),
])
def test_exact_publication_aliases_merge_but_do_not_fuzzy_match_titles(left, right):
    assert reference_keys(left) & reference_keys(right)
    assert not reference_keys(ref("Same title", "https://example.test/one")) & reference_keys(ref("Same title", "https://example.test/two"))


@pytest.mark.parametrize("token", ["[2026]", "[99]", "[3-1]", "[1-999]", "[999999]", "[0]", "[评估999]"])
def test_unresolvable_tokens_and_ordinary_labels_are_not_reinterpreted(token):
    report, source, asset = fixture()
    report["sections"][0]["blocks"][0]["text"] += token
    publish_ordered_references(report, source, asset)
    assert report["sections"][0]["blocks"][0]["text"].endswith(token)
    assert report["sections"][0]["blocks"][0]["title"] == "anchor"


def test_compound_references_keep_encounter_order_and_deduplicate():
    report, source, asset = fixture()
    report["sections"][0]["blocks"][0]["text"] = "Source [3,2,3]."
    publish_ordered_references(report, source, asset)
    assert report["sections"][0]["blocks"][0]["text"] == "Source [1,2]."


def test_transitive_identity_bridge_has_only_one_number():
    report, source, asset = fixture()
    refs = references(report, PRIMARY_TITLE)
    refs.extend([ref("url-only", "https://example.test/bridge"), ref("doi-only", doi="10.1234/bridge"),
                 ref("both", "https://example.test/bridge", doi="10.1234/bridge")])
    report["sections"][0]["blocks"][0]["text"] = "Source [4][5][6]."
    publish_ordered_references(report, source, asset)
    assert report["sections"][0]["blocks"][0]["text"] == "Source [1][1][1]."


def test_fixed_binding_reuses_transitive_doi_url_aliases():
    report, source, asset = fixture()
    asset["appendixBlocks"][0]["items"].extend([
        ref("Bridge", "https://example.test/shared-alternate", doi="10.1234/shared"),
        ref("Alternate URL only", "https://example.test/shared-alternate"),
    ])
    asset["fixedSections"][0]["blocks"][0]["text"] = "One source [1][2][3]."
    publish_ordered_references(report, source, asset)
    assert bind_ordered_static_copy(asset, report)["fixedSections"][0]["blocks"][0]["text"] == "One source [2][2][2]."


def test_frozen_binding_fails_explicitly_if_fixed_source_was_not_planned():
    report, source, asset = fixture()
    publish_ordered_references(report, source, asset)
    references(report, PRIMARY_TITLE).pop()
    with pytest.raises(ValueError, match="missing from frozen"):
        bind_ordered_static_copy(asset, report)


def test_supplemental_source_list_cannot_resolve_an_unknown_old_body_token():
    from adapters.gut_barrier.rendering import _References
    report, source, asset = fixture()
    publish_ordered_references(report, source, asset)
    report["sections"][-1]["blocks"].append({"kind": "references", "title": SUPPLEMENT_TITLE,
        "items": [ref("Historical supplement", "https://example.test/legacy")]})
    refs = _References(report["sections"], primary_number=4, primary_chapter=4, ordered_references=True)
    assert refs.text("Unresolved [补1].") == "Unresolved [补1]."
    assert 'href="#reference-primary-1"' in refs.text("Resolved [1].")


def test_versioned_asset_loader_never_returns_mutable_cached_state(historical_ordered_release):
    first = load_reference_asset(ORDERED_REFERENCE_DISPLAY_TEMPLATE, archive_directory=historical_ordered_release)
    first["fixedSections"].clear()
    assert load_reference_asset(ORDERED_REFERENCE_DISPLAY_TEMPLATE,
                                archive_directory=historical_ordered_release)["fixedSections"]
    with pytest.raises(ValueError, match="exact release"):
        load_reference_asset("../../invalid")


def test_current_reference_loader_uses_real_bundled_asset_and_returns_independent_copies(tmp_path):
    # A caller's archive option cannot replace the production release asset.
    path = Path(__file__).resolve().parents[3] / "adapters" / "gut_barrier" / "assets" / f"{LATEST_DISPLAY_TEMPLATE}.json"
    expected = json.loads(path.read_text(encoding="utf-8"))
    first = load_reference_asset(LATEST_DISPLAY_TEMPLATE, archive_directory=tmp_path)
    assert first == expected
    first["fixedSections"].clear()
    assert load_reference_asset(LATEST_DISPLAY_TEMPLATE) == expected


def test_historical_reference_projection_requires_explicit_archive_without_mutating_source():
    with pytest.raises(FileNotFoundError, match="archive_directory explicitly"):
        load_reference_asset(ORDERED_REFERENCE_DISPLAY_TEMPLATE)
    source = _source()
    source["meta"]["displayTemplateVersion"] = ORDERED_REFERENCE_DISPLAY_TEMPLATE
    before = deepcopy(source)
    with pytest.raises(FileNotFoundError, match="archive_directory explicitly"):
        project_callback_report(source)
    assert source == before


@pytest.mark.parametrize("archive", ["relative-archive", Path(__file__).resolve().parents[3]])
def test_historical_reference_loader_rejects_relative_or_project_archive(archive):
    with pytest.raises(ValueError, match="outside the project"):
        load_reference_asset(ORDERED_REFERENCE_DISPLAY_TEMPLATE, archive_directory=archive)


def test_historical_reference_loader_reports_missing_or_wrong_version_archive(tmp_path):
    with pytest.raises(FileNotFoundError, match="not found"):
        load_reference_asset(ORDERED_REFERENCE_DISPLAY_TEMPLATE, archive_directory=tmp_path)
    path = tmp_path / f"{ORDERED_REFERENCE_DISPLAY_TEMPLATE}.json"
    path.write_text(json.dumps({"templateVersion": LATEST_DISPLAY_TEMPLATE,
                               "reportSchemaVersion": "gut-barrier-public.v2"}), encoding="utf-8")
    with pytest.raises(ValueError, match="version mismatch"):
        load_reference_asset(ORDERED_REFERENCE_DISPLAY_TEMPLATE, archive_directory=tmp_path)


def test_historical_reference_loader_rejects_asset_symlink_outside_archive(tmp_path, symlink_or_skip):
    directory = tmp_path / "archive"
    directory.mkdir()
    target = tmp_path / "external.json"
    target.write_text("{}", encoding="utf-8")
    symlink_or_skip(directory / f"{ORDERED_REFERENCE_DISPLAY_TEMPLATE}.json", target)
    with pytest.raises(ValueError, match="inside its external archive directory"):
        load_reference_asset(ORDERED_REFERENCE_DISPLAY_TEMPLATE, archive_directory=directory)


def test_frozen_historical_report_does_not_read_reference_assets_or_need_archive(monkeypatch):
    from modules.gut_barrier import reference_order

    frozen, _, _ = fixture()
    before = deepcopy(frozen)

    def fail(*args, **kwargs):
        pytest.fail("frozen report must not read any reference asset")

    monkeypatch.setattr(reference_order, "load_reference_asset", fail)
    assert project_callback_report(frozen) == before
    assert project_callback_report(frozen, archive_directory="unused-relative-path") == before
    assert frozen == before


def test_new_publication_callback_and_html_show_only_cited_sources_with_identical_frozen_numbering(historical_ordered_release):
    source = _source(background=True)
    source["meta"]["displayTemplateVersion"] = ORDERED_REFERENCE_DISPLAY_TEMPLATE
    before = deepcopy(source)
    report = project_callback_report(source, archive_directory=historical_ordered_release)
    original = deepcopy(report)
    main = references(report, PRIMARY_TITLE)
    assert [item["citation"] for item in main[:2]] == ["炎症关联资料", "临床评估资料"]
    assert not any(item["citation"] == "干预资料" for item in main)
    assert SUPPLEMENT_TITLE not in json.dumps(report, ensure_ascii=False)
    assert {section["id"] for section in report["sections"]} == {"overview", "disease_risks", "appendix"}
    expanded = expand_dynamic_report(report, template_version=ORDERED_REFERENCE_DISPLAY_TEMPLATE)
    assert expand_dynamic_report(source, template_version=ORDERED_REFERENCE_DISPLAY_TEMPLATE,
                                 template_archive_dir=historical_ordered_release) == expanded
    assert references(expanded, PRIMARY_TITLE) == main
    assert SUPPLEMENT_TITLE not in json.dumps(expanded, ensure_ascii=False)
    assert expand_dynamic_report(expanded, template_version=ORDERED_REFERENCE_DISPLAY_TEMPLATE) == expanded
    html = render_report_html(report, template_version=ORDERED_REFERENCE_DISPLAY_TEMPLATE)
    visible = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", html, flags=re.S)
    first_citations = list(dict.fromkeys(map(int, re.findall(r'href="#reference-primary-(\d+)"', visible))))
    assert first_citations == list(range(1, len(main) + 1))
    assert list(map(int, re.findall(r'class="reference-index">\[(\d+)\]', visible))) == list(range(1, len(main) + 1))
    assert 'id="reference-supplement-' not in visible and SUPPLEMENT_TITLE not in visible
    assert "干预资料" not in visible
    assert set(re.findall(r'href="#(reference-[^"]+)"', visible)) <= set(re.findall(r'id="(reference-[^"]+)"', visible))
    assert json.loads(re.search(r'id="report-data">(.*?)</script>', html, re.S)[1]) == original
    assert project_callback_report(report) == original
    assert project_display_report(report, template_version=ORDERED_REFERENCE_DISPLAY_TEMPLATE)["sourceReport"] == original
    assert report == original and source == before


@pytest.mark.parametrize("items", [[], [ref("Previously frozen supplement", "https://example.test/old")]])
def test_already_frozen_v10_supplements_are_preserved_without_republishing(items, historical_ordered_release):
    source = _source()
    source["meta"]["displayTemplateVersion"] = ORDERED_REFERENCE_DISPLAY_TEMPLATE
    report = project_callback_report(source, archive_directory=historical_ordered_release)
    next(s for s in report["sections"] if s["id"] == "appendix")["blocks"].append(
        {"kind": "references", "title": SUPPLEMENT_TITLE, "items": deepcopy(items)})
    before = deepcopy(report)
    assert project_callback_report(report) == before
    expanded = expand_dynamic_report(report, template_version=ORDERED_REFERENCE_DISPLAY_TEMPLATE)
    assert references(expanded, SUPPLEMENT_TITLE) == items
    assert expand_dynamic_report(expanded, template_version=ORDERED_REFERENCE_DISPLAY_TEMPLATE) == expanded
    html = render_report_html(report, template_version=ORDERED_REFERENCE_DISPLAY_TEMPLATE)
    assert json.loads(re.search(r'id="report-data">(.*?)</script>', html, re.S)[1]) == before
    assert report == before


def test_v9_new_publication_and_frozen_receipt_keep_previous_citation_policy():
    source = _source()
    report = project_callback_report(source)
    assert not any(block.get("title") == SUPPLEMENT_TITLE for s in report["sections"] for block in s["blocks"])
    assert references(report, PRIMARY_TITLE)[0]["citation"] == "干预资料"
    assert project_callback_report(report) == report
