import asyncio
import hashlib
import json
import time
from pathlib import Path

import pymupdf
import pytest

from adapters.report.clients import DocMindTextClient
from modules.report import parsing
from modules.report.clinical import build_abnormal_microbes
from modules.report.clinical import build_source_diseases
from modules.report.clinical import determine_enterotype
from modules.report.evaluation_callback import build_evaluation_callback
from modules.report.assessment_callback_view import assessment_view_for_rendering
from modules.report.evaluation_callback import build_frontend_evaluation_callback
from modules.report.evaluation_schema import EvaluationCallback
from modules.report.parsing import ExtractedDocument
from modules.report.parsing import ExtractedTable
from modules.report.parsing import SourceParseError
from modules.report.parsing import _document_diseases
from modules.report.parsing import _document_metrics
from modules.report.parsing import _document_sample
from modules.report.parsing import _document_subject
from modules.report.parsing import _document_taxa
from modules.report.parsing import _supplementary_hospital_data_from_document
from modules.report.parsing import parse_canonical_data
from modules.report.parsing import parse_downloaded_document
from modules.report.parsing import parse_extracted_text
from modules.report.parsing import parse_provider_data
from modules.report.parsing import parse_request_source
from modules.report.ports import DownloadedFile
from modules.report.ports import DownloadedTestData
from modules.report.schemas import ParsedMicrobiomeReport
from modules.report.schemas import ParsedTaxon
from modules.report.schemas import ReportRequest
from modules.report.schemas import SampleInfo
from modules.report.schemas import SubjectInfo


def request_payload():
    return {
        "userId": "用户唯一ID",
        "sessionId": "346291b2-2166-430f-8352-ec7d9c11e64d",
        "name": "患者姓名",
        "age": "28",
        "sex": "男",
        "fileList": [{"url": "https://m.slmc.top/report.pdf", "fileType": "pdf"}],
        "testData": [],
        "hospitalData": [
            {
                "checkTime": "2025-12-15 09:31:08",
                "dataType": "INSPECTION",
                "itemName": "总胆固醇(TC)",
                "referenceRange": "3.14-5.86",
                "resultStatus": 1,
                "resultStatusName": "偏高",
                "testResultValue": "6.48",
            }
        ],
    }


def test_evaluate_callback_keeps_canonical_source_and_only_prunes_verified_index_aliases():
    def ref(record, date='2026-11-20'):
        return {'sourceCode': 'A', 'recordId': record, 'observedAt': date}

    sources = [
        ref('stable-first'),
        ref('clinicalDocument.facts[0]'),
        ref('clinicalDocument.facts[1]'),
        ref('clinicalDocument.facts[0]', '2026-11-21'),
        ref('clinicalDocument.facts[8]'),
        ref('ambiguous-id'),
        ref('clinicalDocument.facts[2]'),
    ]
    payload = {
        'userId': 'demo-user',
        'sessionId': 'demo-session',
        'status': 'success',
        'generatedAt': '2026-11-22T17:00:00Z',
        'report': {
            'clinicalDocument': {'facts': [
                {'factId': 'stable-first'}, {'factId': 'stable-second'},
                {'factId': 'ambiguous-id'}, {'factId': 'ambiguous-id'},
            ]},
            'evaluationData': {'medicalAssessment': {'findings': [{'value': 0, 'sources': sources}]}},
        },
    }
    callback = build_evaluation_callback(payload)
    row = callback['data']['medicalAssessment']['findings'][0]
    assert row['sources'] == [sources[0], *sources[2:]]
    assert row['value'] == 0
    assert [callback[key] for key in ('type', 'generatedAt', 'reportDate')] == [
        'evaluate', '2026-11-22T17:00:00Z', '2026-11-23',
    ]
    assert len(payload['report']['evaluationData']['medicalAssessment']['findings'][0]['sources']) == 7


def test_documented_evaluate_callback_matches_fixed_contract():
    project_root = Path(__file__).resolve().parents[3]
    # API.md 是当前唯一有效的对外接口契约；history 下的文件是不可变的历史快照，
    # 不应被当前代码模型的单元测试持续约束。
    contract_path = project_root / "docs/api/API.md"
    contract = contract_path.read_text(encoding="utf-8")
    start_marker = "<!-- evaluate-callback-example:start -->"
    end_marker = "<!-- evaluate-callback-example:end -->"
    assert start_marker in contract
    assert end_marker in contract
    documented_example = contract.split(start_marker, 1)[1].split(end_marker, 1)[0]
    json_block = documented_example.split("```json\n", 1)[1].rsplit("\n```", 1)[0]
    payload = json.loads(json_block)

    view = assessment_view_for_rendering(payload['data'])
    assert view is not None
    callback = EvaluationCallback.model_validate({
        **payload,
        'data': {'assessmentPresentation': view, 'appendices': payload['data']['appendices']},
    })
    assert build_frontend_evaluation_callback(callback) == payload
    assert callback.schema_version == 'evaluate.v2.0'
    assert callback.type == 'evaluate'
    assert callback.user_id == 'demo-user'
    assert callback.subject.name == '示例用户'
    assert callback.sample.report_date == '2026-11-10'
    assert callback.report_date.isoformat() == '2026-11-23'
    evidence_numbers = {item.number for item in callback.data.appendices.reference_literature}
    assert set(view['overview']['evidenceRefs']) <= evidence_numbers


def test_failed_evaluate_callback_retains_fixed_header_and_empty_sections():
    callback = build_frontend_evaluation_callback({
        'userId': 'demo-user',
        'sessionId': 'demo-session',
        'status': 'failed',
        'errorCode': 'REPORT_GENERATION_FAILED',
        'message': '报告未能生成有效内容',
    })
    assert callback['type'] == 'evaluate'
    assert callback['schemaVersion'] == 'evaluate.v2.0'
    assert callback['generatedAt'] is None
    assert len(callback['reportDate']) == 10
    assert callback['partialReasons'] == []
    assert callback['error']['errorCode'] == 'REPORT_GENERATION_FAILED'
    assert callback['sample']['reportDate'] is None
    assert callback['data'] == {}


def test_test_environment_uses_expected_report_callback_url():
    project_root = Path(__file__).resolve().parents[3]
    test_env = (project_root / ".env.example.test").read_text(encoding="utf-8")
    assert (
               "SHULAN_REPORT_CALLBACK_URL="
               "https://test.slmc.top/api/basic-health-assessment/ai-full-back/callback"
           ) in test_env.splitlines()


def test_unchanged_request_shape_is_accepted():
    request = ReportRequest.model_validate(request_payload())
    assert request.user_id == "用户唯一ID"
    assert request.age == 28
    assert request.file_list[0].file_type == "pdf"


@pytest.mark.asyncio
async def test_empty_microbiome_sources_are_treated_as_pending_results():
    request = ReportRequest.model_validate(
        {
            "userId": "pending-user",
            "sessionId": "pending-session",
            "name": "待检患者",
            "age": "28",
            "sex": "男",
            "fileList": [],
            "testData": [],
            "hospitalData": [],
        }
    )

    parsed = await parse_request_source(request, source_client=object())

    assert parsed.source_type == "pending"
    assert parsed.subject.name == "待检患者"
    assert parsed.subject.age == 28
    assert parsed.taxa == []
    assert parsed.disease_results == []


@pytest.mark.asyncio
async def test_empty_microbiome_object_is_not_reported_as_a_source_error():
    request = ReportRequest.model_validate(
        {
            "userId": "pending-user",
            "sessionId": "pending-session",
            "microbiomeData": {},
        }
    )

    parsed = await parse_request_source(request, source_client=object())

    assert parsed.source_type == "pending"
    assert parsed.source_errors == []
    assert "菌群检测结果尚在生成中" in (parsed.source_summary or "")
    assert "报告将自动更新" in (parsed.source_summary or "")
    assert "重新生成" not in (parsed.source_summary or "")


def test_multiple_provider_result_pairs_remain_accepted():
    payload = request_payload()
    payload["testData"] = [
        {"resultDetailUrl": f"http://igut-meta.biozeron.com/detail-{index}"}
        for index in range(2)
    ]
    request = ReportRequest.model_validate(payload)
    assert len(request.test_data) == 2


def test_canonical_selection_keeps_explicit_abnormal_and_detected_pathogens():
    request = ReportRequest.model_validate(request_payload())
    payload = {
        "overallMetrics": [{"code": "diversity", "name": "多样性", "value": 2.1}],
        "taxa": [
            {
                "category": "beneficial",
                "latinName": "Akkermansia muciniphila",
                "value": 2.3,
                "status": "正常",
            },
            {
                "category": "beneficial",
                "latinName": "Bifidobacterium longum",
                "value": 0.1,
                "status": "偏低",
            },
            {
                "category": "pathogenic",
                "latinName": "Salmonella enterica",
                "value": "<0.0001",
            },
        ],
    }
    parsed = parse_canonical_data(request, payload)
    section = build_abnormal_microbes(parsed)
    assert [item.scientific_name for item in section.items] == [
        "Bifidobacterium longum",
        "Salmonella enterica",
    ]
    assert section.items[1].source_finding.status == "痕量检出"


def test_provider_data_keeps_all_detected_pathogenic_signals_and_source_diseases():
    request_data = request_payload()
    request_data["fileList"] = []
    request_data["testData"] = [
        {"resultDetailUrl": "http://igut-meta.biozeron.com/detail"}
    ]
    request = ReportRequest.model_validate(request_data)
    detail = {
        "microbeData": {
            "comprehensive": [{"latinName": "Enterotype", "value": "Bacteroides"}],
            "Probiotics": [{"latinName": "Bifidobacterium longum", "value": "3.2"}],
            "Pathogenic": [
                {"latinName": "Salmonella enterica", "value": "0.01"},
                {"latinName": "Shigella flexneri", "value": "0"},
            ],
            "Conditional": [{"latinName": "Klebsiella pneumoniae", "value": "<0.0001"}],
            "Summary_spp": [
                {"latinName": "Probiotics_num", "value": "3"},
                {"latinName": "Pathogenic_num", "value": "1"},
                {"latinName": "Conditioned_Pathogen_num", "value": "2"},
            ],
            "便秘": [{"latinName": "Bacteroides fragilis", "value": "1"}],
        },
        "diseaseRisk": {
            "便秘": "55.56",
            "有益菌": "有益菌总丰度3.2[1, 20]，处于正常状态。",
            "致病菌": "致病菌总丰度0.01[0, 0]，处于偏高状态。",
            "条件致病菌": "条件致病菌总丰度0.0001[0, 0]，处于偏高状态。",
        },
    }
    raw = json.dumps(detail, ensure_ascii=False).encode()
    parsed = parse_provider_data(
        request,
        DownloadedTestData(detail, hashlib.sha256(raw).hexdigest()),
    )
    section = build_abnormal_microbes(parsed)
    assert {item.scientific_name for item in section.items} == {
        "Salmonella enterica",
        "Klebsiella pneumoniae",
    }
    assert parsed.disease_results[0].name == "便秘"
    assert determine_enterotype(parsed).name == "肠型I"
    counts = {item.category: item for item in parsed.panel_counts}
    assert counts["beneficial"].detected_count == 1
    assert counts["beneficial"].source_summary_count == 3
    assert counts["beneficial"].matches_source_summary is False
    assert counts["pathogenic"].matches_source_summary is True
    assert counts["opportunistic"].trace_count == 1


@pytest.mark.asyncio
async def test_structured_source_uses_attached_microbiome_pdf_sample_metadata(
        monkeypatch,
):
    detail = {
        "microbeData": {
            "comprehensive": [],
            "Probiotics": [],
            "Pathogenic": [{"latinName": "Salmonella enterica", "value": "0.01"}],
            "Conditional": [],
        },
        "diseaseRisk": {"便秘": "55.56"},
    }

    class SourceClient:
        async def fetch_test_data(self, source):
            raw = json.dumps(detail, ensure_ascii=False).encode()
            return DownloadedTestData(detail, hashlib.sha256(raw).hexdigest())

        async def fetch_file(self, source):
            return DownloadedFile("report.pdf", "application/pdf", b"pdf", "pdf")

    monkeypatch.setattr(
        "modules.report.parsing.extract_document",
        lambda downloaded: ExtractedDocument(
            filename=downloaded.filename,
            source_type="pdf",
            text="",
            tables=[
                ExtractedTable(
                    page_number=1,
                    rows=[
                        ["样本信息", ""],
                        ["样本编号", "3718407"],
                        ["样本类型", "粪便"],
                        ["检测项目", "肠道菌群检测"],
                        ["采样日期", "2025_07_14"],
                        ["报告日期", "2025_08_04"],
                    ],
                )
            ],
        ),
    )
    payload = request_payload()
    payload["testData"] = [{"resultDetailUrl": "http://igut-meta.biozeron.com/detail"}]
    parsed = await parse_request_source(
        ReportRequest.model_validate(payload),
        source_client=SourceClient(),
    )

    assert parsed.source_type == "test_data"
    assert parsed.taxa[0].latin_name == "Salmonella enterica"
    assert parsed.sample.sample_id == "3718407"
    assert parsed.sample.sample_type == "粪便"
    assert parsed.sample.test_item == "肠道菌群检测"
    assert parsed.sample.sample_date == "2025_07_14"
    assert parsed.sample.report_date == "2025_08_04"


@pytest.mark.asyncio
async def test_multiple_independent_reports_select_latest_without_merging_history():
    details = {
        "http://igut-meta.biozeron.com/old": {
            "microbiomeData": {
                "sample": {
                    "sampleId": "OLD-1",
                    "sampleDate": "2024-01-10",
                    "reportDate": "2024-01-15",
                    "testItem": "肠道菌群检测",
                },
                "taxa": [
                    {
                        "category": "pathogenic",
                        "latinName": "Salmonella enterica",
                        "value": "0.01",
                    }
                ],
                "diseaseRisks": [{"name": "便秘", "score": "45"}],
            }
        },
        "http://igut-meta.biozeron.com/latest": {
            "microbiomeData": {
                "sample": {
                    "sampleId": "NEW-2",
                    "sampleDate": "2025-02-10",
                    "reportDate": "2025-02-15",
                    "testItem": "肠道菌群检测",
                },
                "taxa": [
                    {
                        "category": "opportunistic",
                        "latinName": "Klebsiella pneumoniae",
                        "value": "0.02",
                    }
                ],
                "diseaseRisks": [{"name": "腹胀和排气", "score": "52"}],
            }
        },
    }

    class SourceClient:
        async def fetch_test_data(self, source):
            detail = details[source.result_detail_url]
            raw = json.dumps(detail, ensure_ascii=False).encode()
            return DownloadedTestData(detail, hashlib.sha256(raw).hexdigest())

    payload = request_payload()
    payload["fileList"] = []
    payload["testData"] = [
        {"resultDetailUrl": "http://igut-meta.biozeron.com/old"},
        {"resultDetailUrl": "http://igut-meta.biozeron.com/latest"},
    ]
    parsed = await parse_request_source(
        ReportRequest.model_validate(payload),
        source_client=SourceClient(),
    )

    assert parsed.sample.sample_id == "NEW-2"
    assert parsed.sample.report_date == "2025-02-15"
    assert [item.latin_name for item in parsed.taxa] == ["Klebsiella pneumoniae"]
    assert [item.name for item in parsed.disease_results] == ["腹胀和排气"]
    assert len(parsed.source_history) == 2
    assert sum(item.selected for item in parsed.source_history) == 1


@pytest.mark.asyncio
async def test_undated_dedicated_test_data_is_current_over_dated_upload(
        monkeypatch,
):
    detail = {
        "microbiomeData": {
            "taxa": [
                {
                    "category": "opportunistic",
                    "latinName": "Bacteroides fragilis",
                    "value": "9.744",
                }
            ],
            "diseaseRisks": [{"name": "结直肠癌", "score": "52"}],
        }
    }
    dated_upload = ParsedMicrobiomeReport(
        sourceType="pdf",
        sourceName="older-upload.pdf",
        sourceChecksum="pdf-checksum",
        subject=SubjectInfo(),
        sample=SampleInfo(
            sampleId="OLD-PDF",
            reportDate="2025-08-04",
            testItem="肠道菌群检测",
        ),
        taxa=[
            ParsedTaxon(
                taxonId="taxon-pdf",
                category="opportunistic",
                latinName="Klebsiella pneumoniae",
                value="0.074",
            )
        ],
    )

    class SourceClient:
        async def fetch_test_data(self, source):
            raw = json.dumps(detail, ensure_ascii=False).encode()
            return DownloadedTestData(detail, hashlib.sha256(raw).hexdigest())

        async def fetch_file(self, source):
            return DownloadedFile("older-upload.pdf", "application/pdf", b"pdf", "pdf")

    monkeypatch.setattr(
        "modules.report.parsing.parse_downloaded_document",
        lambda request, downloaded: dated_upload,
    )
    payload = request_payload()
    payload["testData"] = [{"resultDetailUrl": "http://igut-meta.biozeron.com/current"}]
    parsed = await parse_request_source(
        ReportRequest.model_validate(payload),
        source_client=SourceClient(),
    )

    assert parsed.source_type == "test_data"
    assert [item.latin_name for item in parsed.taxa] == ["Bacteroides fragilis"]
    assert [item.name for item in parsed.disease_results] == ["结直肠癌"]
    assert parsed.sample.sample_id is None
    assert len(parsed.source_history) == 2


@pytest.mark.asyncio
async def test_failed_older_source_does_not_block_latest_report():
    latest = {
        "microbiomeData": {
            "sample": {"reportDate": "2025-02-15"},
            "taxa": [
                {
                    "category": "pathogenic",
                    "latinName": "Fusobacterium nucleatum",
                    "value": "0.01",
                }
            ],
        }
    }

    class SourceClient:
        async def fetch_test_data(self, source):
            if source.result_detail_url.endswith("broken"):
                raise RuntimeError("unavailable")
            raw = json.dumps(latest, ensure_ascii=False).encode()
            return DownloadedTestData(latest, hashlib.sha256(raw).hexdigest())

    payload = request_payload()
    payload["fileList"] = []
    payload["testData"] = [
        {"resultDetailUrl": "http://igut-meta.biozeron.com/broken"},
        {"resultDetailUrl": "http://igut-meta.biozeron.com/latest"},
    ]
    parsed = await parse_request_source(
        ReportRequest.model_validate(payload),
        source_client=SourceClient(),
    )

    assert parsed.taxa[0].latin_name == "Fusobacterium nucleatum"
    assert parsed.source_errors == ["testData[0]: RuntimeError"]
    assert len(parsed.source_history) == 1


@pytest.mark.asyncio
async def test_same_report_sources_merge_only_when_dates_match():
    payload = request_payload()
    payload["fileList"] = []
    payload["microbiomeData"] = {
        "sample": {
            "sampleId": "SAME-1",
            "reportDate": "2025-02-15",
        },
        "taxa": [
            {
                "category": "pathogenic",
                "latinName": "Fusobacterium nucleatum",
                "value": "0.01",
            }
        ],
    }
    supplemental = {
        "microbiomeData": {
            "sample": {
                "sampleId": "SAME-1",
                "reportDate": "2025-02-15",
            },
            "taxa": [
                {
                    "category": "opportunistic",
                    "latinName": "Klebsiella pneumoniae",
                    "value": "0.02",
                }
            ],
        }
    }

    class SourceClient:
        async def fetch_test_data(self, source):
            raw = json.dumps(supplemental, ensure_ascii=False).encode()
            return DownloadedTestData(supplemental, hashlib.sha256(raw).hexdigest())

    payload["testData"] = [{"resultDetailUrl": "http://igut-meta.biozeron.com/same"}]
    parsed = await parse_request_source(
        ReportRequest.model_validate(payload),
        source_client=SourceClient(),
    )

    assert parsed.source_type == "microbiome_data"
    assert {item.latin_name for item in parsed.taxa} == {
        "Fusobacterium nucleatum",
        "Klebsiella pneumoniae",
    }


def test_pdf_text_metrics_and_source_enterotype_are_parsed():
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_textbox(
        pymupdf.Rect(40, 40, 500, 500),
        (
            "肠道微生物健康指数\n-0.02\n菌群多样性\n7.36\n"
            "肠道微生物种类\n1372\n肠型\nII\n"
            "种类\n总检出丰度\n结果评价\n有益菌\n9.4395%\n"
            "种类\n总检出丰度\n结果评价\n有害菌\n15.1029%"
        ),
        fontsize=12,
        # Use PyMuPDF's built-in Simplified Chinese font so this fixture is
        # portable across macOS, Linux containers, and CI runners.
        fontname="china-s",
    )
    content = pdf.tobytes()
    pdf.close()
    request = ReportRequest.model_validate(request_payload())
    parsed = parse_downloaded_document(
        request,
        DownloadedFile("sample.pdf", "application/pdf", content, "pdf"),
    )
    assert parsed.enterotype == "肠型II"
    metrics = {item.code: item for item in parsed.metrics}
    assert metrics["species_count"].value == "1372"
    assert metrics["panel_beneficial"].status == "中度缺乏"
    assert metrics["panel_harmful"].status == "重度超标"


def test_linear_ocr_metric_preserves_status_after_numeric_value():
    request = ReportRequest.model_validate(
        {"userId": "u-status", "sessionId": "s-status"}
    )

    parsed = parse_extracted_text(
        request,
        filename="page.jpg",
        source_type="image",
        text=(
            "肠道微生物健康指数 -0.34 亚健康\n"
            "菌群多样性 4.96 正常\n"
            "肠道微生物种类 1248 正常"
        ),
    )

    metrics = {item.code: item for item in parsed.metrics}
    assert metrics["gmhi"].status == "亚健康"
    assert metrics["diversity"].status == "正常"
    assert metrics["species_count"].status == "正常"


def test_provider_assessment_table_overrides_toc_numbers_and_keeps_ranges():
    document = ExtractedDocument(
        filename="reference.pdf",
        source_type="pdf",
        text=("目录 肠道菌群多样性评估 1.1.8 肠道菌群致病潜力评估 1.1.4"),
        tables=[
            ExtractedTable(
                page_number=8,
                rows=[
                    ["检测项目", "评估值", "参考范围", "结果评价"],
                    ["肠道菌群多样性评估", "7.0757", "2-10", "正常"],
                    [
                        "肠道菌群致病潜力评估",
                        "1.8901",
                        "2.5-11.662",
                        "失衡",
                    ],
                ],
            ),
            ExtractedTable(
                page_number=8,
                rows=[
                    ["短链脂肪酸", "评估值", "参考范围", "合成能力评估"],
                    ["乙酸", "57", ">60", "合成能力弱"],
                    ["丁酸", "87", ">60", "合成能力强"],
                ],
            ),
        ],
    )

    metrics = {item.code: item for item in _document_metrics(document)}

    assert metrics["diversity"].value == "7.0757"
    assert metrics["diversity"].status == "正常"
    assert metrics["pathogenic_potential"].value == "1.8901"
    assert metrics["pathogenic_potential"].reference_range == "2.5-11.662"
    assert metrics["acetate"].value == "57"
    assert metrics["acetate"].status == "合成能力弱"
    assert metrics["butyrate"].value == "87"


def test_pdf_table_status_and_cross_page_taxon_are_preserved():
    document = ExtractedDocument(
        filename="sample.pdf",
        source_type="pdf",
        text="",
        tables=[
            ExtractedTable(
                page_number=1,
                context="有益菌",
                rows=[
                    [
                        "嗜热链球菌 Streptococcus thermophilus",
                        "说明",
                        "0.0151",
                        "丰度低",
                    ]
                ],
            ),
            ExtractedTable(
                page_number=2,
                context="条件致病菌",
                rows=[["小韦荣球菌", "说明前半", "0.1085", "检出"]],
            ),
            ExtractedTable(
                page_number=3,
                context="",
                rows=[["Veillonella parvula", "说明后半", ""]],
            ),
        ],
    )
    taxa = _document_taxa(document)
    assert [(item.latin_name, item.source_status) for item in taxa] == [
        ("Streptococcus thermophilus", "丰度低"),
        ("Veillonella parvula", "检出"),
    ]
    assert taxa[0].explicitly_abnormal is True
    assert taxa[1].value == "0.1085"


def test_metagenomic_panel_tables_preserve_sections_arrows_and_chinese_names():
    document = ExtractedDocument(
        filename="metagenomic.pdf",
        source_type="pdf",
        text="",
        tables=[
            ExtractedTable(
                page_number=1,
                context="有益菌检出结果",
                rows=[
                    ["拉丁学名", "中文名称", "检测结果(%)", "参考范围"],
                    [
                        "Akkermansia muciniphila",
                        "嗜粘蛋白-艾克曼菌",
                        "4.565↑",
                        "0-2.4003",
                    ],
                    ["Eubacterium rectale", "直肠真杆菌", "0.0178↓", "0.0355-6.4379"],
                ],
            ),
            ExtractedTable(
                page_number=2,
                context="致病菌检测结果",
                rows=[
                    ["拉丁学名", "中文名称", "检测结果(%)", "参考范围", "结果评价"],
                    ["Salmonella enterica", "-", "<0.0001", "0-0.001", "正常"],
                ],
            ),
            ExtractedTable(
                page_number=3,
                context="条件致病菌检测结果",
                rows=[
                    ["拉丁学名", "中文名称", "检测结果(%)", "参考范围"],
                    ["Bacteroides fragilis", "脆弱拟杆菌", "0.0209", "0-3.1892"],
                ],
            ),
            # The later authoritative panel must replace an earlier ungraded
            # duplicate so its explicit arrow is not lost.
            ExtractedTable(
                page_number=4,
                context="部分和健康相关的其他菌种检测结果",
                rows=[
                    ["拉丁学名", "中文名称", "检测结果(%)", "参考范围", "结果评价"],
                    ["Bacteroides fragilis", "脆弱拟杆菌", "0.0209↑", "0-0", "超标"],
                    [
                        "Dorea formicigenerans",
                        "蚁酸生成多雷氏 菌",
                        "0.0018↓",
                        "0.0026-0.4313",
                        "缺乏",
                    ],
                ],
            ),
        ],
    )
    taxa = _document_taxa(document)
    by_name = {item.latin_name: item for item in taxa}
    assert by_name["Akkermansia muciniphila"].source_status == "超标"
    assert by_name["Eubacterium rectale"].source_status == "缺乏"
    assert by_name["Salmonella enterica"].chinese_name is None
    assert by_name["Bacteroides fragilis"].category == "other"
    assert by_name["Bacteroides fragilis"].explicitly_abnormal is True
    assert by_name["Dorea formicigenerans"].chinese_name == "蚁酸生成多雷氏菌"


@pytest.mark.parametrize("source_type", ["pdf", "image"])
def test_semantic_panel_supports_combined_names_flattened_rows_and_reference_direction(source_type):
    document = ExtractedDocument(
        filename=f"unseen-provider.{source_type}",
        source_type=source_type,
        text=(
            "\n===== PAGE 1 =====\n姓名\n王琳\n年龄\n36岁\n性别\n女\n"
            "\n===== PAGE 2 =====\n疾病相似评估\n新的肠道疾病\n0.34\n注意\n"
            "新的代谢病\n0.10\n低相似\n"
        ),
        tables=[
            ExtractedTable(
                page_number=1,
                context="基本信息",
                rows=[
                    ["姓名", "王琳", "编号", "x"],
                    ["年龄", "36岁", "性别", "女"],
                ],
            ),
            ExtractedTable(
                page_number=3,
                context="核心菌属构成表",
                rows=[
                    ["名称", "丰度%", "正常范围%", "处于人群%水平"],
                    ["未配置菌属 Novelbacter 8.5\uf176 0-4.0 99%", "", "", ""],
                    ["新有益菌 Newbeneficus 0.01 0.02-3.0 20%", "", "", ""],
                    ["普通菌 Normalibacter 1.2 0-2.0 50%", "", "", ""],
                ],
            ),
        ],
    )

    subject = _document_subject(document)
    taxa = {item.latin_name: item for item in _document_taxa(document)}
    diseases = _document_diseases(document)

    assert subject == SubjectInfo(name="王琳", age=36, sex="女")
    assert taxa["Novelbacter"].value == "8.5"
    assert taxa["Novelbacter"].source_status == "超标"
    assert taxa["Newbeneficus"].source_status == "缺乏"
    assert taxa["Normalibacter"].explicitly_abnormal is False
    assert [(item.name, item.source_level) for item in diseases] == [
        ("新的肠道疾病", "中风险"),
        ("新的代谢病", "低风险"),
    ]


def test_subject_does_not_treat_subject_information_heading_as_name():
    document = ExtractedDocument(
        filename="image.png",
        source_type="image",
        text="受检者信息\n姓名\n许先生\n性别\n男\n年龄\n48岁9个月",
        tables=[
            ExtractedTable(
                page_number=1,
                context="受检者信息",
                rows=[["姓名", "", "许先生"], ["性别", "男"], ["年龄", "48岁9个月"]],
            )
        ],
    )

    assert _document_subject(document) == SubjectInfo(name="许先生", age=48, sex="男")


def test_pdf_sample_uses_metadata_table_and_does_not_shift_labels():
    document = ExtractedDocument(
        filename="sample.pdf",
        source_type="pdf",
        text=(
            "检测项目：肠道菌群检测\n"
            "检测编号：3718407\n"
            "样本类型：粪便\n"
            "检测日期：2025_07_14\n"
            "报告日期：2025_08_04\n"
            "其他页面\n检测项目\n评估值\n"
        ),
        tables=[
            ExtractedTable(
                page_number=2,
                context="",
                rows=[["检测项目", "评估值"]],
            )
        ],
    )
    sample = _document_sample(document)
    assert sample.sample_id == "3718407"
    assert sample.sample_type == "粪便"
    assert sample.test_item == "肠道菌群检测"
    assert sample.sample_date == "2025_07_14"
    assert sample.report_date == "2025_08_04"


def test_pdf_disease_cards_add_clear_zero_results_and_keep_typo_for_withholding():
    document = ExtractedDocument(
        filename="sample.pdf",
        source_type="pdf",
        text=(
            "\n===== PAGE 1 =====\n"
            "便秘\n疾病风险评估\n便秘 \n风险为:\n55.56\n中风险\n"
            "\n===== PAGE 2 =====\n"
            "双向情感障碍\n疾病风险评估\n双向情感障碍 \n风险为:\n0.0\n低风险\n"
        ),
        tables=[
            ExtractedTable(
                page_number=1,
                context="慢性疾病风险评估",
                rows=[
                    ["疾病名称", "风险指数", "风险等级", "正负相关菌"],
                    ["便秘", "55.56", "中风险", "另枝菌属(超标)"],
                ],
            )
        ],
    )
    diseases = _document_diseases(document)
    assert [(item.name, item.source_score) for item in diseases] == [
        ("便秘", "55.56"),
        ("双向情感障碍", "0.0"),
    ]
    assert diseases[0].related_taxa == ["另枝菌属(超标)"]


def test_pdf_disease_placeholder_table_preserves_source_rows_without_guessing_scores():
    document = ExtractedDocument(
        filename="provider-template.pdf",
        source_type="pdf",
        text=(
            "慢性疾病风险评估\n"
            "{炎症性肠道疾病}\n"
            "炎症性肠道疾病是一类慢性非特异性肠黏膜炎症性疾病。\n"
            "主要包括克罗恩病和溃疡性结肠炎。\n"
            "{结直肠癌}\n结直肠癌是结肠或直肠的恶性肿瘤。"
        ),
        tables=[
            ExtractedTable(
                page_number=15,
                context="慢性疾病风险评估",
                rows=[
                    ["炎症性肠道疾病", "<ibd_png> _", "<ibd>"],
                    ["结直肠癌", "<crc_png> _", "<crc>"],
                    ["腹泻", "<diarrhea_png> _", "<diarrhea>"],
                    ["重金属", "<HeavyMetal_png> _", "<HeavyMetal>"],
                ],
            )
        ],
    )

    diseases = _document_diseases(document)

    assert [item.name for item in diseases] == ["炎症性肠道疾病", "结直肠癌", "腹泻"]
    assert all(item.source_score is None for item in diseases)
    assert all(item.source_level is None for item in diseases)
    assert diseases[0].source_description == (
        "炎症性肠道疾病是一类慢性非特异性肠黏膜炎症性疾病。"
        "主要包括克罗恩病和溃疡性结肠炎。"
    )
    section = build_source_diseases(
        ParsedMicrobiomeReport(
            sourceType="pdf",
            sourceName="provider-template.pdf",
            subject=SubjectInfo(),
            diseaseResults=diseases,
        ),
        [],
    )
    assert section.items[0].source_description.startswith("炎症性肠道疾病")
    assert section.items[0].medical_interpretation is None


@pytest.mark.asyncio
async def test_image_uses_docmind_text_when_local_ocr_is_unavailable():
    class ImageSourceClient:
        async def fetch_file(self, source):
            return DownloadedFile(
                "microbiome.png", "image/png", b"not-an-image", "image"
            )

    class DocumentClient:
        async def extract_text(self, source, *, filename, content=None):
            return (
                "1.7.1 条件致病菌\n"
                "大肠埃希氏菌\n"
                "Escherichia coli\n"
                "检测说明\n"
                "1.8509\n"
                "检出\n"
            )

    payload = request_payload()
    payload["fileList"] = [
        {"url": "https://example.com/microbiome.png", "fileType": "image"}
    ]
    parsed = await parse_request_source(
        ReportRequest.model_validate(payload),
        source_client=ImageSourceClient(),
        document_client=DocumentClient(),
    )
    assert parsed.source_type == "image"
    assert parsed.taxa[0].latin_name == "Escherichia coli"
    assert parsed.taxa[0].detection_status == "detected"


@pytest.mark.asyncio
async def test_non_microbiome_checkup_pdf_becomes_pending_with_clinical_facts():
    class CheckupSourceClient:
        async def fetch_file(self, source):
            return DownloadedFile("checkup.pdf", "application/pdf", b"scan", "pdf")

    class DocumentClient:
        async def extract_text(self, source, *, filename, content=None):
            return (
                "姓名：周舒 性别：男 年龄：41岁\n"
                "体检日期：2025-09-19\n"
                "身高：183 cm BMI指数：33.7 体重：113 kg\n"
                "体脂肪 (kg) 40.6\n"
                "骨骼肌 (kg) 41.4\n"
                "肝脏硬度(kPa) 中位数 9.4\n"
                "脂肪衰减(dB/m) 中位数 350\n"
                "人体成分测定：脂肪过量、体脂百分比肥胖、腹部肥胖、BMI严重超重。"
            )

    request = ReportRequest.model_validate(
        {
            "userId": "u-checkup",
            "sessionId": "s-checkup",
            "name": "周舒",
            "age": 41,
            "sex": "男",
            "fileList": [{"url": "https://example.com/checkup.pdf", "fileType": "pdf"}],
        }
    )

    parsed = await parse_request_source(
        request,
        source_client=CheckupSourceClient(),
        document_client=DocumentClient(),
    )

    assert parsed.source_type == "pending"
    assert parsed.taxa == []
    assert parsed.disease_results == []
    clinical = {item.item_name: item for item in parsed.supplementary_hospital_data}
    assert clinical["身高"].test_result_value == "183 cm"
    assert clinical["体重"].test_result_value == "113 kg"
    assert clinical["BMI"].test_result_value == "33.7 kg/m²"
    assert clinical["BMI"].result_status_name == "严重超重"
    assert clinical["体脂率（由体脂肪量和体重计算）"].test_result_value == "35.9 %"
    assert clinical["体脂率（由体脂肪量和体重计算）"].result_status_name == "肥胖"
    assert clinical["骨骼肌量"].test_result_value == "41.4 kg"
    assert clinical["肝脏硬度"].test_result_value == "9.4 kPa"
    assert clinical["脂肪衰减"].test_result_value == "350 dB/m"
    assert "上传体检报告摘要" not in clinical


@pytest.mark.asyncio
async def test_scan_pdf_falls_back_to_existing_external_document_service(monkeypatch):
    calls: list[str] = []
    local_calls: list[str] = []

    class CheckupSourceClient:
        async def fetch_file(self, source):
            return DownloadedFile("scan.pdf", "application/pdf", b"scan", "pdf")

    class DocumentClient:
        async def extract_text(self, source, *, filename, content=None):
            calls.append(filename)
            assert content == b"scan"
            return (
                "报告日期：2026-08-03\n"
                "| 检验项目 | 结果 | 单位 | 参考范围 | 状态 |\n"
                "| --- | --- | --- | --- | --- |\n"
                "| 全新项目S | 9.8 | U/L | 1.0-5.0 | 偏高 |\n"
            )

    def fail_local(_request, downloaded):
        local_calls.append(downloaded.filename)
        raise SourceParseError("scan-only local parse unavailable")

    monkeypatch.setattr(parsing, "parse_downloaded_document", fail_local)
    request = ReportRequest.model_validate(
        {
            "userId": "scan-user",
            "sessionId": "scan-session",
            "fileList": [
                {"url": "https://example.com/scan.pdf", "fileType": "pdf"}
            ],
        }
    )

    parsed = await parse_request_source(
        request,
        source_client=CheckupSourceClient(),
        document_client=DocumentClient(),
    )

    assert local_calls == ["scan.pdf"]
    assert calls == ["scan.pdf"]
    assert parsed.source_type == "pending"
    assert any(
        item.item_name == "全新项目S"
        for item in parsed.supplementary_hospital_data
    )


@pytest.mark.asyncio
async def test_local_clinical_pdf_precedes_external_flattened_ocr(monkeypatch):
    external_calls: list[str] = []

    class CheckupSourceClient:
        async def fetch_file(self, source):
            return DownloadedFile("checkup.pdf", "application/pdf", b"pdf", "pdf")

    class DocumentClient:
        async def extract_text(self, source, *, filename, content=None):
            external_calls.append(filename)
            return "低密度脂蛋白胆固醇 (LDL-c) 0.99 g/L"

    monkeypatch.setattr(
        parsing,
        "parse_downloaded_document",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SourceParseError("clinical-only document")
        ),
    )
    monkeypatch.setattr(
        parsing,
        "extract_document",
        lambda _downloaded: ExtractedDocument(
            filename="checkup.pdf",
            source_type="pdf",
            text="姓名：许迪龙 性别：男 年龄：37岁 体检日期：2025-08-13",
            tables=[
                ExtractedTable(
                    page_number=1,
                    rows=[
                        ["项目名称", "结果", "参考范围", "单位"],
                        ["低密度脂蛋白胆固醇", "3.40", "0-3.40", "mmol/L"],
                        ["载脂蛋白B", "0.99", "0.60-1.10", "g/L"],
                    ],
                )
            ],
        ),
    )
    request = ReportRequest.model_validate(
        {
            "userId": "local-clinical-user",
            "sessionId": "local-clinical-session",
            "fileList": [
                {"url": "https://example.com/checkup.pdf", "fileType": "pdf"}
            ],
        }
    )

    parsed = await parse_request_source(
        request,
        source_client=CheckupSourceClient(),
        document_client=DocumentClient(),
    )

    values = {
        item.item_name: item.test_result_value
        for item in parsed.supplementary_hospital_data
    }
    assert external_calls == []
    assert values["低密度脂蛋白胆固醇"] == "3.40 mmol/L"
    assert values["载脂蛋白B"] == "0.99 g/L"


@pytest.mark.asyncio
async def test_hybrid_pdf_keeps_complete_local_microbiome_parse(monkeypatch):
    external_calls: list[str] = []

    class SourceClient:
        async def fetch_file(self, source):
            return DownloadedFile("hybrid.pdf", "application/pdf", b"hybrid", "pdf")

    class DocumentClient:
        async def extract_text(self, source, *, filename, content=None):
            external_calls.append(filename)
            return "报告日期：2026-08-03"

    def local_parse(request, downloaded):
        return ParsedMicrobiomeReport(
            sourceType="pdf",
            sourceName=downloaded.filename,
            subject=SubjectInfo(name="本地受检者"),
            taxa=[
                ParsedTaxon(
                    taxonId="taxon-local",
                    category="beneficial",
                    latinName="Faecalibacterium prausnitzii",
                    chineseName="普拉梭菌",
                    value="0.1",
                    explicitlyAbnormal=True,
                    detectionStatus="detected",
                )
            ],
        )

    monkeypatch.setattr(parsing, "parse_downloaded_document", local_parse)
    request = ReportRequest.model_validate(
        {
            "userId": "hybrid-user",
            "sessionId": "hybrid-session",
            "fileList": [
                {"url": "https://example.com/hybrid.pdf", "fileType": "pdf"}
            ],
        }
    )

    parsed = await parse_request_source(
        request,
        source_client=SourceClient(),
        document_client=DocumentClient(),
    )

    assert [item.chinese_name for item in parsed.taxa] == ["普拉梭菌"]
    assert external_calls == []


@pytest.mark.asyncio
async def test_clinical_only_pdf_enriches_subject_and_report_date(monkeypatch):
    class SourceClient:
        async def fetch_file(self, source):
            return DownloadedFile("checkup.pdf", "application/pdf", b"scan", "pdf")

    class DocumentClient:
        async def extract_text(self, source, *, filename, content=None):
            return (
                "姓名：刘杰\n性别：男 年龄：49岁\n体检日期：2024-11-02\n"
                "| 检验项目 | 结果 | 单位 | 参考范围 | 状态 |\n"
                "| --- | --- | --- | --- | --- |\n"
                "| 甘油三酯 | 2.21 | mmol/L | 0-1.70 | 偏高 |\n"
            )

    monkeypatch.setattr(
        parsing,
        "parse_downloaded_document",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SourceParseError("scan-only local parse unavailable")
        ),
    )
    request = ReportRequest.model_validate(
        {
            "userId": "clinical-user",
            "sessionId": "clinical-session",
            "fileList": [
                {"url": "https://example.com/checkup.pdf", "fileType": "pdf"}
            ],
        }
    )

    parsed = await parse_request_source(
        request,
        source_client=SourceClient(),
        document_client=DocumentClient(),
    )

    assert parsed.subject == SubjectInfo(name="刘杰", age=49, sex="男")
    assert parsed.sample.report_date == "2024-11-02"
    assert any(
        item.item_name == "甘油三酯"
        for item in parsed.supplementary_hospital_data
    )


def test_repeated_laboratory_column_groups_are_both_extracted():
    document = ExtractedDocument(
        filename="double-column.pdf",
        source_type="pdf",
        text="体检日期：2024-11-02",
        tables=[
            ExtractedTable(
                1,
                [
                    [
                        "项目名称",
                        "结果",
                        "参考范围",
                        "单位",
                        "项目名称",
                        "结果",
                        "参考范围",
                        "单位",
                    ],
                    [
                        "总胆固醇",
                        "5.13",
                        "0-5.2",
                        "mmol/L",
                        "甘油三酯",
                        "2.21↑",
                        "0-1.70",
                        "mmol/L",
                    ],
                ],
            )
        ],
    )

    rows = _supplementary_hospital_data_from_document(document)
    values = {item.item_name: item for item in rows}

    assert values["总胆固醇"].test_result_value == "5.13 mmol/L"
    assert values["甘油三酯"].test_result_value == "2.21 mmol/L"
    assert values["甘油三酯"].result_status_name == "偏高"


def test_implausible_ocr_derived_body_fat_is_not_published():
    document = ExtractedDocument(
        filename="body.pdf",
        source_type="pdf",
        text=(
            "体检日期：2024-11-02\n"
            "体重：91.7 kg\n"
            "体脂肪 (kg) 85.0\n"
        ),
    )

    rows = _supplementary_hospital_data_from_document(document)

    assert all("由体脂肪量" not in item.item_name for item in rows)


@pytest.mark.asyncio
async def test_external_scan_facts_are_stable_across_repeated_parsing(monkeypatch):
    class ScanSourceClient:
        async def fetch_file(self, source):
            return DownloadedFile("repeat.pdf", "application/pdf", b"scan", "pdf")

    class DocumentClient:
        async def extract_text(self, source, *, filename, content=None):
            return (
                "报告日期：2026-08-03\n"
                "| 项目 | 结果 | 单位 | 参考范围 | 状态 |\n"
                "| --- | --- | --- | --- | --- |\n"
                "| 从未配置过的指标甲 | 9.8 | U/L | 1.0-5.0 | 偏高 |\n"
                "体检结论\n"
                "1. 未知版式中的明确异常结论。\n"
            )

    monkeypatch.setattr(
        parsing,
        "parse_downloaded_document",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SourceParseError("scan-only local parse unavailable")
        ),
    )
    request = ReportRequest.model_validate(
        {
            "userId": "stable-scan-user",
            "sessionId": "stable-scan-session",
            "fileList": [
                {"url": "https://example.com/repeat.pdf", "fileType": "pdf"}
            ],
        }
    )

    results = [
        await parse_request_source(
            request,
            source_client=ScanSourceClient(),
            document_client=DocumentClient(),
        )
        for _ in range(5)
    ]
    normalized = [
        [
            item.model_dump(mode="json", by_alias=True)
            for item in parsed.supplementary_hospital_data
        ]
        for parsed in results
    ]

    assert normalized == [normalized[0]] * 5
    assert {item["itemName"] for item in normalized[0]} >= {
        "从未配置过的指标甲",
        "未知版式中的明确异常结论",
    }


def test_complete_checkup_document_extracts_abnormal_results_and_main_conclusions():
    document = ExtractedDocument(
        filename="checkup.pdf",
        source_type="pdf",
        text=(
            "姓名:许先生\n体检日期: 2026-06-30\n"
            "体重指数:29.3 kg/m2\n腰围:92 cm\n"
            "总胆固醇(TC):5.88 mmol/L ↑ 3.14-5.86\n"
            "空腹C肽(C-P)测定:4.97 ng/ml ↑ 1.00-4.80\n"
            "隐血试验:弱阳性 B 阴性\n"
            "糖化血红蛋白\nA1C\n5.75\n4.00-6.00\n%\n"
            "肌酐(Cr)\n77\n57-97\nμmol/L\n"
            "尿酸(UA)\n387\n208-428\nμmol/L\n"
            "肾小球滤过率\n(GFR-Cr)\n102.005\n-\nmL/min\n"
        ),
        tables=[
            ExtractedTable(
                page_number=2,
                rows=[["1、", "", "左侧甲状腺结节 TI-RADS 3类"]],
            ),
            ExtractedTable(
                page_number=2,
                rows=[["2、", "", "空腹C肽降低: 4.97 ng/ml"]],
            ),
        ],
    )

    clinical = _supplementary_hospital_data_from_document(document)
    by_name = {item.item_name: item for item in clinical}

    assert by_name["BMI"].test_result_value == "29.3 kg/m²"
    assert by_name["总胆固醇(TC)"].result_status_name == "偏高"
    assert by_name["空腹C肽(C-P)测定"].result_status_name == "偏高"
    assert by_name["隐血试验"].test_result_value == "弱阳性"
    assert by_name["糖化血红蛋白"].test_result_value == "5.75 %"
    assert by_name["肌酐(Cr)"].test_result_value == "77 μmol/L"
    assert by_name["尿酸(UA)"].reference_range == "208-428"
    assert by_name["肾小球滤过率(GFR-Cr)"].test_result_value == (
        "102.005 mL/min"
    )
    assert by_name["左侧甲状腺结节 TI-RADS 3类"].exam_result
    assert "空腹C肽降低" not in by_name


def test_scan_only_pdf_uses_page_ocr_instead_of_counting_page_markers(monkeypatch):
    class Rect:
        width = 100
        height = 100

    class Tables:
        tables = []

    class Page:
        rect = Rect()

        def get_text(self, mode, *, textpage=None):
            if mode == "blocks":
                return []
            return "未见固定模板检查项 12.6 mmol/L 偏高" if textpage else ""

        def get_textpage_ocr(self, **kwargs):
            assert kwargs["language"] == "chi_sim+eng"
            return object()

        def get_images(self, *, full):
            assert full is True
            return []

        def find_tables(self):
            return Tables()

    class Document:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def __iter__(self):
            return iter([Page()])

    monkeypatch.setattr(parsing.pymupdf, "open", lambda **kwargs: Document())

    document = parsing._extract_pdf("scan.pdf", b"scan")

    assert "未见固定模板检查项" in document.text
    assert document.metadata["ocr_pages"] == "1"


def test_generic_clinical_table_extracts_unseen_test_names_with_provenance():
    document = ExtractedDocument(
        filename="unseen-hospital.pdf",
        source_type="pdf",
        text="检测日期：2026-08-01",
        tables=[
            ExtractedTable(
                page_number=7,
                rows=[
                    ["检验项目", "检测结果", "单位", "参考范围", "状态"],
                    ["未见固定清单的新指标XYZ", "18.2", "U/L", "3.0-12.0", "偏高"],
                ],
            )
        ],
    )

    rows = _supplementary_hospital_data_from_document(document)
    result = next(item for item in rows if item.item_name.endswith("XYZ"))

    assert result.test_result_value == "18.2 U/L"
    assert result.reference_range == "3.0-12.0"
    assert result.result_status_name == "偏高"
    assert result.source_file == "unseen-hospital.pdf"
    assert result.source_page == 7


def test_generic_summary_extracts_numbered_findings_without_item_whitelist():
    document = ExtractedDocument(
        filename="unknown-layout.pdf",
        source_type="pdf",
        text=(
            "===== PAGE 3 =====\n"
            "本次体检主要异常结果\n"
            "1.左侧未知部位影像信号：建议结合专科检查。\n"
            "2.某项功能检查异常，请关注变化趋势。\n"
            "健康建议\n请规律作息。"
        ),
    )

    rows = _supplementary_hospital_data_from_document(document)
    findings = [item for item in rows if item.data_type == "EXAM"]

    assert any("左侧未知部位影像信号" in item.item_name for item in findings)
    assert any("某项功能检查异常" in (item.exam_result or "") for item in findings)
    assert not any("规律作息" in (item.exam_result or "") for item in findings)


def test_docmind_markdown_and_inline_results_are_parsed_generically():
    document = ExtractedDocument(
        filename="docmind-output.pdf",
        source_type="pdf",
        text=(
            "报告日期：2026-08-02\n"
            "全新血液指标Q：18.2↑ U/L\n\n"
            "| 检验项目 | 检测结果 | 单位 | 参考值 | 提示 |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| 陌生项目R | 0.72 | mg/L | 0.10-0.50 | 偏高 |\n\n"
            "| 结论 | 建议 |\n"
            "| --- | --- |\n"
            "| 某部位结节，建议结合专科评估。 | 定期复查 |\n"
        ),
    )

    rows = _supplementary_hospital_data_from_document(document)
    by_name = {item.item_name: item for item in rows}

    assert by_name["全新血液指标Q"].result_status_name == "偏高"
    assert by_name["陌生项目R"].test_result_value == "0.72 mg/L"
    assert by_name["陌生项目R"].reference_range == "0.10-0.50"
    assert by_name["某部位结节"].exam_result == "某部位结节，建议结合专科评估。"


def test_docmind_markdown_microbiome_tables_use_semantic_headers():
    text = (
        "姓名：谢某\n检测编号：3718419\n报告日期：2025-09-05\n"
        "1.7.1条件致病菌\n"
        "条件致病菌是在特定条件下可能影响健康的菌群。\n"
        "| 菌名 | 功能说明 | 检测结果(%) | 结果评价 |\n"
        "| --- | --- | --- | --- |\n"
        "| 大肠埃希氏菌 Escherichia coli | 来源说明 | 2.2893 | 检出 |\n"
        "| 小韦荣球菌 Veillonella<br>parvula | 来源说明 | 0.7531 | 检出 |\n"
        "| 青春双歧杆菌 Bifidobacteriumadolescentis | 来源说明 | 0.0088 | 丰度低 |\n"
        "| 短双歧杆菌 Bifidobacterium breve | 来源说明 | — | 未检出 |\n"
        "| 未检出菌 Example absentis | 来源说明 | — | 未检出 |\n"
        "9.肠道菌群与疾病相关风险评估\n"
        "| 名称 | 风险指数 | 风险评估 |\n"
        "| --- | --- | --- |\n"
        "| 便秘 | 0.2152 | 低风险 |\n"
        "16S肠道微生物基因检测报告\n12\n"
        "| 肥胖症 | D<br>0.4173 | 低风险 |\n"
        "16S肠道微生物基因检测报告\n13\n"
        "| 2型糖尿病 | [0.5552) | 中风险 |\n"
        "16S肠道微生物基因检测报告\n14\n"
        "| 抑郁症 | 0.6823<br>11 | 中风险 |\n"
        "0.4871酒精肝低风险\n"
        "肠道微生物检测结果分析\n"
    )

    parsed = parse_extracted_text(
        ReportRequest.model_validate(
            {"userId": "u-markdown", "sessionId": "s-markdown"}
        ),
        filename="unseen-layout.pdf",
        source_type="pdf",
        text=text,
    )

    assert [(item.latin_name, item.category) for item in parsed.taxa] == [
        ("Escherichia coli", "opportunistic"),
        ("Veillonella parvula", "opportunistic"),
        ("Bifidobacterium adolescentis", "opportunistic"),
    ]
    assert [item.name for item in parsed.disease_results] == [
        "便秘",
        "肥胖症",
        "2型糖尿病",
        "抑郁症",
        "酒精肝",
    ]
    assert [item.source_score for item in parsed.disease_results] == [
        "0.2152",
        "0.4173",
        "0.5552",
        "0.6823",
        "0.4871",
    ]


def test_function_metrics_ignore_contents_numbers_and_descriptive_mentions():
    document = ExtractedDocument(
        filename="microbiome.pdf",
        source_type="pdf",
        text=(
            "短链脂肪酸合成评估\n1.2. 肠道菌群精准分布\n"
            "某菌可促进肠道屏障功能成熟，并编码260种酶。\n"
            "肠道抗炎能力 62\n肠道免疫力 94\n"
            "肠道抗炎能力评估值分为强、正常、弱共3类，"
            "对应分值区间为>80、60-80、<60。"
        ),
    )

    metrics = {item.code: item for item in _document_metrics(document)}

    assert "gut_barrier" not in metrics
    assert "scfa" not in metrics
    assert metrics["anti_inflammatory"].value == "62"
    assert metrics["anti_inflammatory"].status == "正常"
    assert metrics["gut_immunity"].value == "94"
    assert metrics["gut_immunity"].status == "强"


@pytest.mark.asyncio
async def test_local_checkup_pdf_keeps_clinical_facts_before_sample_metadata(
    monkeypatch,
):
    class CheckupSourceClient:
        async def fetch_file(self, source):
            return DownloadedFile("checkup.pdf", "application/pdf", b"scan", "pdf")

    document = ExtractedDocument(
        filename="checkup.pdf",
        source_type="pdf",
        text=(
            "姓名:许先生\n体检日期: 2026-06-30\n"
            "体重指数:29.3 kg/m2\n"
            "总胆固醇(TC):5.88 mmol/L ↑ 3.14-5.86\n"
        ),
        tables=[
            ExtractedTable(
                page_number=2,
                rows=[["1、", "", "左侧甲状腺结节 TI-RADS 3类"]],
            )
        ],
    )

    def reject_microbiome(*args, **kwargs):
        raise parsing.SourceParseError("not a microbiome report")

    monkeypatch.setattr(parsing, "parse_downloaded_document", reject_microbiome)
    monkeypatch.setattr(parsing, "extract_document", lambda downloaded: document)
    request = ReportRequest.model_validate(
        {
            "userId": "u-checkup-local",
            "sessionId": "s-checkup-local",
            "name": "许先生",
            "fileList": [
                {"url": "https://example.com/checkup.pdf", "fileType": "pdf"}
            ],
        }
    )

    parsed = await parse_request_source(
        request,
        source_client=CheckupSourceClient(),
    )

    assert parsed.source_type == "pending"
    clinical = {item.item_name: item for item in parsed.supplementary_hospital_data}
    assert clinical["总胆固醇(TC)"].result_status_name == "偏高"
    assert clinical["左侧甲状腺结节 TI-RADS 3类"].exam_result


@pytest.mark.asyncio
async def test_docmind_uses_downloaded_image_extension_not_public_image_type():
    class Client:
        def __init__(self):
            self.submitted = None

        def submit_online_file(self, url, *, file_name, file_type):
            self.submitted = (url, file_name, file_type)
            return "task-1"

        def get_result(self, task_id, poll_interval):
            assert task_id == "task-1"
            return {"success": True, "text": "菌群多样性 4.96 正常"}

    document_client = object.__new__(DocMindTextClient)
    document_client.timeout_seconds = 1
    document_client.client = Client()
    source = ReportRequest.model_validate(
        {
            "userId": "u1",
            "sessionId": "s1",
            "fileList": [
                {
                    "url": "https://example.com/preview?objectName=report.jpg",
                    "fileType": "image",
                }
            ],
        }
    ).file_list[0]

    text = await document_client.extract_text(source, filename="report.jpg")

    assert text == "菌群多样性 4.96 正常"
    assert document_client.client.submitted == (
        source.url,
        "report.jpg",
        "jpg",
    )


@pytest.mark.asyncio
async def test_docmind_submits_downloaded_bytes_as_temporary_local_file():
    class Client:
        def __init__(self):
            self.submitted = None
            self.local_path = None

        def submit_local_file(self, file_path, *, file_name, file_type):
            self.local_path = Path(file_path)
            assert self.local_path.exists()
            assert self.local_path.read_bytes() == b"scan-pdf-bytes"
            self.submitted = (file_name, file_type)
            return "task-local"

        def submit_online_file(self, *args, **kwargs):
            pytest.fail("downloaded bytes must not be fetched again by URL")

        def get_result(self, task_id, poll_interval):
            assert task_id == "task-local"
            return {"success": True, "text": "甲状腺结节 TI-RADS 3类"}

    document_client = object.__new__(DocMindTextClient)
    document_client.timeout_seconds = 1
    document_client.client = Client()
    source = ReportRequest.model_validate(
        {
            "userId": "u-local",
            "sessionId": "s-local",
            "fileList": [
                {
                    "url": "https://example.com/private-scan.pdf",
                    "fileType": "pdf",
                }
            ],
        }
    ).file_list[0]

    text = await document_client.extract_text(
        source,
        filename="private-scan.pdf",
        content=b"scan-pdf-bytes",
    )

    assert text == "甲状腺结节 TI-RADS 3类"
    assert document_client.client.submitted == ("private-scan.pdf", "pdf")
    assert document_client.client.local_path is not None
    assert not document_client.client.local_path.exists()


@pytest.mark.asyncio
async def test_docmind_reuses_identical_downloaded_bytes_in_process():
    class Client:
        def __init__(self):
            self.submissions = 0

        def submit_local_file(self, file_path, *, file_name, file_type):
            self.submissions += 1
            return "task-cached"

        def get_result(self, task_id, poll_interval):
            time.sleep(0.02)
            return {"success": True, "text": "扫描件识别结果"}

    document_client = object.__new__(DocMindTextClient)
    document_client.timeout_seconds = 1
    document_client.client = Client()
    source = ReportRequest.model_validate(
        {
            "userId": "u-cache",
            "sessionId": "s-cache",
            "fileList": [
                {
                    "url": "https://example.com/repeated.pdf",
                    "fileType": "pdf",
                }
            ],
        }
    ).file_list[0]

    first, second = await asyncio.gather(
        document_client.extract_text(
            source,
            filename="repeated.pdf",
            content=b"same-scan",
        ),
        document_client.extract_text(
            source,
            filename="repeated.pdf",
            content=b"same-scan",
        ),
    )
    third = await document_client.extract_text(
        source,
        filename="renamed.pdf",
        content=b"same-scan",
    )

    assert first == second == third == "扫描件识别结果"
    assert document_client.client.submissions == 1


@pytest.mark.asyncio
async def test_consecutive_report_images_are_reconstructed_before_parsing():
    texts = {
        "page-1.jpg": (
            "肠道总体评分\n您的肠道总评分为：\n58\n中度失衡\n"
            "肠道微生物健康指数 -0.34\n菌群多样性 4.96\n"
            "肠道微生物种类 1248\n有益菌 5.4840%\n有害菌 5.2454%"
        ),
        "page-2.jpg": (
            "2.肠道免疫力评估\n肠道抗炎能力 50\n肠道免疫力 46\n"
            "检测结果<30，评价结果为弱；检测结果30-60，评价结果为稍弱；"
            "检测结果>60，评价结果为正常。"
        ),
        "page-3.jpg": (
            "4.短链脂肪酸合成能力\n乙酸 59\n丙酸 45\n丁酸 47\n"
            "检测结果<30，评价结果为弱；检测结果30-60，评价结果为稍弱；"
            "检测结果>60，评价结果为正常。"
        ),
    }

    class ImageSourceClient:
        async def fetch_file(self, source):
            name = source.url.rsplit("/", 1)[-1]
            return DownloadedFile(name, "image/jpeg", name.encode(), "image")

    class DocumentClient:
        async def extract_text(self, source, *, filename, content=None):
            return texts[filename]

    request = ReportRequest.model_validate(
        {
            "userId": "u-images",
            "sessionId": "s-images",
            "fileList": [
                {"url": f"https://example.com/{name}", "fileType": "image"}
                for name in texts
            ],
        }
    )
    parsed = await parse_request_source(
        request,
        source_client=ImageSourceClient(),
        document_client=DocumentClient(),
    )

    metrics = {item.code: item for item in parsed.metrics}
    assert parsed.source_name == "page-1.jpg..page-3.jpg"
    assert metrics["gut_overall_score"].value == "58"
    assert metrics["panel_beneficial"].value == "5.4840"
    assert metrics["panel_harmful"].value == "5.2454"
    assert metrics["anti_inflammatory"].value == "50"
    assert metrics["anti_inflammatory"].status == "稍弱"
    assert metrics["anti_inflammatory"].reference_range == ">60"
    assert metrics["gut_immunity"].value == "46"
    assert metrics["gut_immunity"].reference_range == ">60"
    assert metrics["acetate"].value == "59"
    assert metrics["acetate"].reference_range == ">60"
    assert metrics["propionate"].value == "45"
    assert metrics["propionate"].reference_range == ">60"
    assert metrics["butyrate"].value == "47"
    assert metrics["butyrate"].reference_range == ">60"
    assert len(parsed.source_history) == 1
    assert parsed.source_history[0].selected is True


@pytest.mark.asyncio
async def test_new_report_header_prevents_unrelated_image_reports_from_merging():
    texts = {
        "report-a.jpg": "肠道总体评分\n您的肠道总评分为：\n58\n菌群多样性 4.96",
        "report-b.jpg": "肠道总体评分\n您的肠道总评分为：\n72\n菌群多样性 5.31",
    }

    class ImageSourceClient:
        async def fetch_file(self, source):
            name = source.url.rsplit("/", 1)[-1]
            return DownloadedFile(name, "image/jpeg", name.encode(), "image")

    class DocumentClient:
        async def extract_text(self, source, *, filename, content=None):
            return texts[filename]

    request = ReportRequest.model_validate(
        {
            "userId": "u-separate",
            "sessionId": "s-separate",
            "fileList": [
                {"url": f"https://example.com/{name}", "fileType": "image"}
                for name in texts
            ],
        }
    )
    parsed = await parse_request_source(
        request,
        source_client=ImageSourceClient(),
        document_client=DocumentClient(),
    )

    assert ".." not in parsed.source_name
    assert len(parsed.source_history) == 2
