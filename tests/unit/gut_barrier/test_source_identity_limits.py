"""Synthetic fixtures: source names never filter data; PDF limits still apply."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pymupdf
import pytest

from adapters.gut_barrier import sources as source_module
from adapters.gut_barrier.sources import GutBarrierSources, extract_observations
from modules.gut_barrier.schemas import GutBarrierRequest


@pytest.mark.parametrize("label", [
    "受检者：李明·26", "受检者：李明男·26", "受检者：李明·男·26",
    "受检者：李明 男 26岁", "受检者：李明 性别：男 年龄：26",
    "受检者姓名：李明", "受检者姓名 李明", "受 检 者：李明·26",
    "受检人：李明", "被检者：李明", "姓 名：李明", "姓名：李明性别：男年龄：26",
    "姓名：李明\n受检者：李明·26", "受检者：李明 姓名：李明", "受检者：\n李明·26",
])
def test_subject_labels_do_not_affect_observation_parsing(label):
    observed, issues = extract_observations(
        label + "\n采样日期：2026-09-24\nDAO 13 U/L", source_id="synthetic",
        today="2026-09-24",
    )
    assert issues == []
    assert len(observed) == 1 and observed[0]["isCurrent"] is True


@pytest.mark.parametrize("header", [
    "受检者：另一患者·26", "受检者：另一患者男·26", "受检者姓名：另一患者",
    "姓名：李明\n受检者：另一患者·26", "受检者：李明 姓名：另一患者",
])
def test_explicit_other_or_mixed_subjects_do_not_discard_observations(header):
    observed, issues = extract_observations(
        header + "\nDAO 13 U/L", source_id="synthetic",
    )
    assert len(observed) == 1 and observed[0]["value"] == "13"
    assert observed[0]["source"]["identityVerified"] is False
    assert issues == [{"code": "SOURCE_SAMPLE_DATE_UNVERIFIED", "sourceId": "synthetic"}]
    assert "另一患者" not in json.dumps(issues, ensure_ascii=False)


@pytest.mark.parametrize("header", ["受检者须知", "李明男·26", "受检者健康教育"])
def test_unlabelled_cover_and_subject_instructions_do_not_exclude_results(header):
    observations, issues = extract_observations(header + "\nDAO 13 U/L", source_id="s")
    assert len(observations) == 1
    assert observations[0]["source"]["identityVerified"] is False
    assert all("IDENTITY" not in issue["code"] for issue in issues)


@pytest.mark.parametrize("name", ["王男", "阿卜杜拉·热合曼", "Jane Doe"])
def test_source_names_do_not_assert_verified_identity(name):
    observations, _ = extract_observations("受检者：" + name + "·26\nDAO 13 U/L", source_id="s")
    assert observations[0]["source"]["identityVerified"] is False


def synthetic_pdf(pages):
    with pymupdf.open() as document:
        for text in pages:
            page = document.new_page()
            if text:
                page.insert_text((30, 40), text, fontname="china-s", fontsize=11)
        return document.tobytes()


async def collect_pdf(content, parser=None):
    source = GutBarrierSources(
        downloader=SimpleNamespace(download=AsyncMock(return_value=content)), parsing_client=parser,
    )
    request = GutBarrierRequest(userId="offline", sessionId="limits", name="李明", fileList=[{
        "url": "https://offline.invalid/synthetic.pdf", "fileType": "pdf",
    }])
    return await source.collect(request, evaluation_date="2026-09-24")


@pytest.mark.asyncio
async def test_below_limit_other_subject_keeps_markers_and_microbiome():
    content = synthetic_pdf([
        "体检报告\n受检者：另一患者·26\n手机：13900000000",
        "DAO 13 U/L\n肠菌健康指数 -0.04",
    ])
    result = await collect_pdf(content)
    assert [(row["code"], row["value"]) for row in result["observations"]] == [("DAO", "13")]
    assert result["context"]["microbiomeSources"][0]["metrics"][0]["value"] == "-0.04"
    assert result["sources"][0]["status"] == "parsed"
    assert result["sources"][0]["auxiliaryCount"] == 1
    assert {issue["code"] for issue in result["issues"]} == {"SOURCE_SAMPLE_DATE_UNVERIFIED"}
    assert "另一患者" not in json.dumps(result["issues"], ensure_ascii=False)


@pytest.mark.asyncio
async def test_unlabelled_native_cover_does_not_guess_a_name_even_when_it_looks_like_one():
    # Some PDF labels are raster while their values are native text. Without
    # OCR/label linkage an isolated value is not verified patient metadata.
    result = await collect_pdf(synthetic_pdf([
        "肠道菌群宏基因组健康评估报告\n性别：\n2026-09-24\n另一患者\n男",
        "目录",
    ]))
    assert not any(issue["code"] == "SOURCE_IDENTITY_CONFLICT" for issue in result["issues"])
    assert not result["observations"]


@pytest.mark.asyncio
@pytest.mark.parametrize("header", ["受检者：李明·26", "受检者：\n李明·26"])
async def test_matching_subject_on_cover_applies_to_later_native_page(header):
    result = await collect_pdf(synthetic_pdf([
        header + "\n采样日期：2026-09-24", "DAO 13 U/L",
    ]))
    assert result["issues"] == []
    assert result["observations"][0]["isCurrent"] is True
    assert result["observations"][0]["source"]["page"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("subject", ["李明", "另一患者", None])
async def test_oversized_pdf_only_reads_two_native_preview_pages_and_never_extracts(monkeypatch, subject):
    monkeypatch.setattr(source_module, "MAX_DOCUMENT_PAGES", 2)
    header = "NGS 肠道菌群报告\n" + (f"受检者：{subject}·26\n" if subject else "")
    content = synthetic_pdf([header + "手机：13900000000\nDAO 13 U/L", "hs-CRP 4.2 mg/L", "DAO 30 U/L", "肠菌健康指数 1"])
    seen, native = [], pymupdf.Page.get_text

    def bounded_text(page, *args, **kwargs):
        seen.append(page.number)
        assert page.number < 2, "No full-document extraction after the page limit"
        return native(page, *args, **kwargs)

    monkeypatch.setattr(pymupdf.Page, "get_text", bounded_text)
    parser = Mock()
    result = await collect_pdf(content, parser)
    assert seen == [0, 1]
    assert result["observations"] == [] and result["context"] == {}
    summary = result["sources"][0]
    assert summary["pageCount"] == 4 and summary["pageLimit"] == 2
    assert summary["previewOnly"] is True and summary["previewPageCount"] == 2
    assert "previewIdentityStatus" not in summary
    assert summary["documentType"] == "microbiome_report"
    assert summary["status"] == "page_limit_exceeded"
    assert summary["observationCount"] == summary["auxiliaryCount"] == 0
    assert result["issues"][0]["code"] == "SOURCE_PAGE_LIMIT_EXCEEDED"
    assert result["issues"][0]["pageCount"] == 4 and result["issues"][0]["pageLimit"] == 2
    assert not any("IDENTITY" in issue["code"] for issue in result["issues"])
    assert not any(issue["code"] == "SOURCE_UNAVAILABLE" for issue in result["issues"])
    assert parser.mock_calls == []
    serialized = json.dumps(result, ensure_ascii=False)
    assert "另一患者" not in serialized and "13900000000" not in serialized
    assert "李明" not in serialized and "DAO" not in serialized


@pytest.mark.asyncio
async def test_preview_failure_preserves_known_page_limit_and_does_not_leak_error_text(monkeypatch):
    monkeypatch.setattr(source_module, "MAX_DOCUMENT_PAGES", 2)
    content = synthetic_pdf(["cover", "", "DAO 30 U/L"])
    native = pymupdf.Page.get_text

    def broken_first_page(page, *args, **kwargs):
        if page.number == 0:
            raise ValueError("private-patient-and-phone")
        return native(page, *args, **kwargs)

    monkeypatch.setattr(pymupdf.Page, "get_text", broken_first_page)
    result = await collect_pdf(content)
    assert result["sources"][0]["status"] == "page_limit_exceeded"
    assert result["sources"][0]["pageCount"] == 3
    assert {row["code"] for row in result["issues"]} == {"SOURCE_PAGE_LIMIT_EXCEEDED", "SOURCE_PREVIEW_UNAVAILABLE"}
    assert "private-patient-and-phone" not in str(result)


def test_preview_does_not_relax_existing_per_page_ocr_guard(monkeypatch):
    monkeypatch.setattr(source_module, "MAX_DOCUMENT_PAGES", 2)
    with pytest.raises(ValueError, match="invalid PDF page"):
        GutBarrierSources._pdf_page_bytes(synthetic_pdf(["cover", "", "DAO 30 U/L"]), 0)
