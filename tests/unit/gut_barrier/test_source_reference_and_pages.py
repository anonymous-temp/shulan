"""Synthetic-only regression cases for result/reference columns and page OCR."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pymupdf
import pytest

from adapters.gut_barrier import sources as source_module
from adapters.gut_barrier.sources import GutBarrierSources, extract_observations
from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.schemas import GutBarrierRequest


HEADER = "姓名：李明\n采样日期：2026-09-24\n"


def extracted(text):
    return extract_observations(HEADER + text, source_id="synthetic", today="2026-09-24")


@pytest.mark.parametrize("row", [
    "参考值 DAO 5 U/L", "正常参考 DAO 5 U/L", "reference DAO 5 U/L",
    "参考范围：D-Lactate <3 mg/L", "正常值：D-Lactate 5 mg/L",
    "参考范围\nDAO 5 U/L", "项目 | 参考值 | 单位\nDAO | 5 | U/L",
    "项目 | 结果 | 单位\n参考值 DAO | 5 | U/L",
])
def test_reference_only_rows_never_become_measured_values(row):
    observations, issues = extracted(row)
    assert observations == []
    assert any(issue["code"] == "AMBIGUOUS_MARKER_ROW" for issue in issues)


@pytest.mark.parametrize("row", [
    "示例 DAO 3 U/L", "例如 D-Lactate 0.3 mg/L", "方法学 DAO 3 U/L",
    "DAO 方法学 3 U/L", "Example DAO result 3 U/L", "Methodology DAO 3 U/L",
    "方法学\nDAO 3 U/L", "示例\n项目 | 结果 | 单位\nDAO | 3 | U/L",
    "项目 | 结果 | 单位\n示例 DAO | 3 | U/L", "项目 | 结果 | 单位\n方法学 DAO | 3 | U/L",
    "项目 | 结果 | 单位\nDAO | 3 | U/L（示例）",
])
def test_examples_and_method_prose_cannot_become_current_results_even_with_identity_and_date(row):
    observations, issues = extracted(row)
    assert observations == []
    assert any(issue["code"] == "AMBIGUOUS_MARKER_ROW" for issue in issues)


@pytest.mark.parametrize("row,expected", [
    ("DAO 17 U/L 参考范围 0-8.5 U/L", "17"),
    ("DAO 17 U/L（正常参考 0-8.5）", "17"),
    ("DAO 结果：17 U/L，参考值 0-8.5 U/L", "17"),
    ("参考范围 DAO 0-8.5 U/L；检测结果：17 U/L", "17"),
    ("DAO 参考范围 0-8.5 U/L；实测值 17 U/L", "17"),
    ("参考范围\nDAO 5 U/L\n检测结果\nDAO 17 U/L", "17"),
    ("项目 | 参考范围 | 结果 | 单位\nDAO | 0-8.5 | 17 | U/L", "17"),
    ("项目\t检测结果\t单位\t参考值\nDAO\t17\tU/L\t0-8.5", "17"),
    ("项目  单位  参考范围  实测值\nDAO  U/L  0-8.5  17", "17"),
    ("项目 结果 单位 参考值\nDAO 17 U/L 0-8.5", "17"),
    ("检测方法：酶法\n项目 | 结果 | 单位\nDAO | 17 | U/L", "17"),
    ("DAO 17 U/L；检测方法：酶法", "17"),
    ("方法学\nDAO 3 U/L\n本次结果\nDAO 17 U/L", "17"),
])
def test_measured_result_is_retained_when_reference_values_are_also_present(row, expected):
    observations, _ = extracted(row)
    assert [(item["code"], item["value"]) for item in observations] == [("DAO", expected)]
    assert observations[0]["isCurrent"] is True


@pytest.mark.parametrize("row", [
    "项目 | 参考值 | 结果 | 单位\nDAO | 5 | -- | U/L",
    "项目 | 参考值 | 结果 | 单位\nDAO | 5 | U/L",
    "项目 | 参考值 | 结果 | 单位\nDAO | 5 | 17 | U/L | extra",
])
def test_missing_or_lost_result_column_does_not_fall_back_to_reference_number(row):
    observations, _ = extracted(row)
    assert observations == []


def test_reference_marker_cannot_borrow_another_analytes_labelled_result():
    observations, _ = extracted("参考值 DAO 5 U/L；LPS 结果 23 U/L")
    assert [(item["code"], item["value"]) for item in observations] == [("LPS", "23")]


@pytest.mark.parametrize("retired", [
    "hs-CRP", "hsCRP", "CRP", "超敏C反应蛋白", "超敏C反應蛋白",
    "high sensitivity C reactive protein", "highSensitivityCRP", "ＣＲＰ",
])
@pytest.mark.parametrize("result,expected", [("未检测", None), ("14.2 mg/L", "14.2")])
def test_retired_assay_never_supplies_or_overwrites_an_active_assay_result(retired, result, expected):
    observations, issues = extracted(f"D-乳酸 {result}；{retired} 检测结果：30 mg/L；LPS 结果23 U/L")
    expected_rows = [("LPS", "23")] if expected is None else [("D_LACTATE", expected), ("LPS", "23")]
    assert [(item["code"], item["value"]) for item in observations] == expected_rows
    assert [issue["marker"] for issue in issues] == (["D_LACTATE"] if expected is None else [])


def test_retired_assay_before_active_result_or_in_another_table_row_does_not_hide_it():
    for text in (
        "hs-CRP 检测结果30 mg/L；D-乳酸 检测结果14.2 mg/L",
        "项目 | 结果 | 单位\nhs-CRP | 30 | mg/L\nD-乳酸 | 14.2 | mg/L",
    ):
        observations, issues = extracted(text)
        assert [(item["code"], item["value"]) for item in observations] == [("D_LACTATE", "14.2")]
        assert issues == []


def test_four_reference_rows_cannot_create_false_normal_score():
    observations, _ = extracted("参考范围\nD-Lactate 5 mg/L\nDAO 5 U/L\nLPS 5 U/L\nD-Lactate 0.5 mg/L")
    assessment = assess_case(GutBarrierRequest(userId="offline", sessionId="s", name="李明"),
                             observations=observations, context={"currentDate": "2026-09-24"})
    assert assessment["barrier"]["score"] is None
    assert assessment["barrier"]["status"] == "incomplete"
    assert "inflammation" not in assessment


def synthetic_pdf(pages):
    """Create in-memory dummy pages; no user PDFs or external image files."""
    image = pymupdf.Pixmap(pymupdf.csRGB, (0, 0, 32, 32), False)
    image.clear_with(245)
    with pymupdf.open() as document:
        for text, scanned in pages:
            page = document.new_page()
            if text:
                page.insert_text((35, 35), text)
            if scanned:
                page.insert_image(pymupdf.Rect(35, 80, 550, 750), stream=image.tobytes("png"))
        return document.tobytes()


def test_genuinely_blank_page_does_not_require_ocr_but_scan_does():
    content = synthetic_pdf([("DAO 17 U/L", False), ("", False), ("", True)])
    pages = GutBarrierSources._pdf_pages(content)
    assert pages.ocr_pages == frozenset({2})
    assert len(pages) == 3


class LocalParser:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.submitted = []

    def submit_local_file(self, file_path, *, file_name=None, file_type=None):
        with pymupdf.open(file_path) as document:
            assert len(document) == 1  # No whole-document reparse of readable pages.
            self.submitted.append({"name": file_name, "native": document[0].get_text(), "path": file_path})
        return str(len(self.submitted) - 1)

    def get_result(self, task_id, *, poll_interval=1):
        result = self.outcomes[int(task_id)]
        if isinstance(result, BaseException):
            raise result
        return {"success": True, "text": result}


async def collect_pdf(content, parser=None, **options):
    source = GutBarrierSources(downloader=SimpleNamespace(download=AsyncMock(return_value=content)),
                              parsing_client=parser, **options)
    request = GutBarrierRequest(userId="offline", sessionId="pages", name="李明", fileList=[{
        "url": "https://offline.invalid/synthetic.pdf", "fileType": "pdf",
    }])
    return await source.collect(request, evaluation_date="2026-09-24")


@pytest.mark.asyncio
async def test_mixed_text_and_scanned_pages_parse_only_the_scanned_page_with_original_page_number():
    parser = LocalParser([HEADER + "D-Lactate 2.7 mg/L"])
    result = await collect_pdf(synthetic_pdf([("DAO 17 U/L", False), ("", True)]), parser)
    assert [(row["code"], row["source"]["page"], row["source"]["method"]) for row in result["observations"]] == [
        ("DAO", 1, "native_text"), ("D_LACTATE", 2, "ocr")]
    assert all(row["isCurrent"] for row in result["observations"])
    assert result["issues"] == []
    assert result["sources"][0]["failedPages"] == []
    assert [row["ocrStatus"] for row in result["sources"][0]["pageAudit"]] == ["not_required", "parsed"]
    assert [row["name"] for row in parser.submitted] == ["document-page-2.pdf"]
    assert all(not Path(row["path"]).exists() for row in parser.submitted)


@pytest.mark.asyncio
async def test_text_header_does_not_hide_scanned_results_on_the_same_page():
    parser = LocalParser([HEADER + "D-Lactate 26 mg/L"])
    result = await collect_pdf(synthetic_pdf([("Report header only", True)]), parser)
    assert len(parser.submitted) == 1
    assert [row["code"] for row in result["observations"]] == ["D_LACTATE"]
    assert result["sources"][0]["pageAudit"][0]["ocrRequired"] is True


@pytest.mark.asyncio
async def test_identical_native_and_ocr_values_keep_both_provenance_locations():
    parser = LocalParser([HEADER + "DAO 17 U/L"])
    result = await collect_pdf(synthetic_pdf([("DAO 17 U/L", True)]), parser)
    assert len(result["observations"]) == 1
    assert {item["method"] for item in result["observations"][0]["source"]["locations"]} == {"native_text", "ocr"}
    assert result["issues"] == []


@pytest.mark.asyncio
async def test_conflicting_native_and_ocr_results_are_preserved_and_not_silently_overwritten():
    parser = LocalParser([HEADER + "DAO 23 U/L"])
    result = await collect_pdf(synthetic_pdf([("DAO 17 U/L", True)]), parser)
    assert {item["value"] for item in result["observations"]} == {"17", "23"}
    assert any(issue["code"] == "SOURCE_MARKER_VALUE_CONFLICT" for issue in result["issues"])
    assessed = assess_case(GutBarrierRequest(userId="u", sessionId="s", name="李明"),
                           observations=result["observations"], context={"currentDate": "2026-09-24"})
    dao = next(item for item in assessed["markers"] if item["code"] == "DAO")
    assert dao["value"] is None and dao["status"] == "needs_review"


@pytest.mark.asyncio
async def test_partial_ocr_failure_keeps_other_pages_and_audits_failed_page_without_secret_text():
    parser = LocalParser([RuntimeError("secret-patient-token"), HEADER + "DAO 17 U/L"])
    result = await collect_pdf(synthetic_pdf([("D-Lactate 26 mg/L", False), ("", True), ("", True)]), parser)
    assert {item["code"] for item in result["observations"]} == {"D_LACTATE", "DAO"}
    assert result["sources"][0]["status"] == "partially_parsed"
    assert result["sources"][0]["failedPages"] == [2]
    assert any(issue.get("page") == 2 and issue["code"] == "SOURCE_UNAVAILABLE" for issue in result["issues"])
    assert "secret-patient-token" not in str(result)


@pytest.mark.asyncio
async def test_missing_ocr_does_not_discard_readable_page_or_mark_document_fully_parsed():
    result = await collect_pdf(synthetic_pdf([("DAO 17 U/L", False), ("", True)]))
    assert [item["code"] for item in result["observations"]] == ["DAO"]
    assert result["sources"][0]["status"] == "partially_parsed"
    assert result["sources"][0]["failedPages"] == [2]
    assert any(issue["code"] == "OCR_UNAVAILABLE" and issue["page"] == 2 for issue in result["issues"])


@pytest.mark.asyncio
async def test_later_ocr_name_difference_keeps_all_document_values():
    parser = LocalParser(["姓名：其他患者\n采样日期：2026-09-24\nD-Lactate 2.7 mg/L"])
    result = await collect_pdf(synthetic_pdf([("DAO 17 U/L", False), ("", True)]), parser)
    assert {(row["code"], row["value"]) for row in result["observations"]} == {("DAO", "17"), ("D_LACTATE", "2.7")}
    assert result["sources"][0]["status"] == "parsed"
    assert result["issues"] == []
    assert all(row["source"]["identityVerified"] is False for row in result["observations"])


@pytest.mark.asyncio
async def test_reference_section_state_continues_across_pages(monkeypatch):
    content = synthetic_pdf([("reference range", False), ("DAO 5 U/L", False)])
    monkeypatch.setattr(GutBarrierSources, "_pdf_pages", staticmethod(lambda _: [HEADER + "参考范围", "DAO 5 U/L"]))
    result = await collect_pdf(content)
    assert result["observations"] == []
    assert any(issue["code"] == "AMBIGUOUS_MARKER_ROW" for issue in result["issues"])


@pytest.mark.asyncio
async def test_ocr_page_limit_preserves_completed_pages_and_marks_unprocessed_pages(monkeypatch):
    monkeypatch.setattr(source_module, "MAX_OCR_PAGES", 1)
    parser = LocalParser([HEADER + "DAO 17 U/L"])
    result = await collect_pdf(synthetic_pdf([("", True)] * 3), parser)
    assert len(parser.submitted) == 1
    assert result["sources"][0]["failedPages"] == [2, 3]
    assert len([issue for issue in result["issues"] if issue["code"] == "OCR_PAGE_LIMIT_REACHED"]) == 2


@pytest.mark.asyncio
async def test_oversized_ocr_page_does_not_replace_readable_native_values(monkeypatch):
    monkeypatch.setattr(source_module, "MAX_DOCUMENT_TEXT", 100)
    parser = LocalParser(["x" * 101])
    result = await collect_pdf(synthetic_pdf([("DAO 17 U/L", False), ("", True)]), parser)
    assert [row["code"] for row in result["observations"]] == ["DAO"]
    assert result["sources"][0]["failedPages"] == [2]
    assert any(issue.get("errorType") == "ValueError" for issue in result["issues"])


@pytest.mark.asyncio
async def test_ocr_cancellation_propagates_instead_of_claiming_partial_success():
    parser = LocalParser([asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await collect_pdf(synthetic_pdf([("", True)]), parser)
