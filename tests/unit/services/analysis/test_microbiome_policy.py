"""Document native-source v1 stays fixed when Report independently evolves."""

import hashlib
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from adapters.analysis import microbiome_source
from adapters.analysis.microbiome_source import MICROBIOME_SOURCE_REVISION
from adapters.analysis.microbiome_source import read_microbiome_source_facts
from modules.analysis.application._microbiome_v1 import document_source
from modules.analysis.application._microbiome_v1 import parsing
from modules.analysis.application._microbiome_v1 import source_context
from tests.unit._support.native_report_pdf import native_pdf
from tests.unit.services.analysis.test_analysis_json import interpreter_with


def read(content, **options):
    return read_microbiome_source_facts(content=content, text='', file_name='native.pdf', docmind=None, **options)


def test_native_pdf_preserves_literal_values_dates_and_fixed_source_evidence():
    content = native_pdf()
    facts = read(content)
    assert facts == read(content, implementation_revision=MICROBIOME_SOURCE_REVISION)
    assert facts.title == '肠道菌群检测报告' and facts.report_time == '2026-09-01'
    assert [(row['itemName'], row['resultValue'], row['referenceRange']) for row in facts.observations] == [
        ('肠道菌群多样性', '7.36', '5-8'),
        ('肠道微生物健康指数', '-0.02', '0-1'),
    ]
    for row in facts.observations:
        assert row['observedAt'] == '2026-09-01'
        assert row['sourceId'] == row['evidence']['sourceId']
        assert row['sourceId'].startswith('nativepdf:')
        assert row['evidence']['locator']['sourceChecksum'] == hashlib.sha256(content).hexdigest()
        assert row['evidence']['locator']['physicalPage'] == 1


def test_report_function_and_schema_changes_cannot_change_document_cold_results(monkeypatch):
    from modules.report import document_source as report_source
    from modules.report import parsing as report_parsing
    from modules.report import schemas as report_schemas
    from modules.report import source_context as report_context

    content = native_pdf()
    expected = asdict(read(content))
    bomb = Mock(side_effect=AssertionError('Report implementation must not execute'))
    for module, names in (
        (report_parsing, ('_extract_pdf', '_explicit_report_date', '_cell', 'original_amr_table_rows')),
        (report_source, ('strict_report_source_records', 'strict_report_source_diseases', 'printed_table_taxa')),
        (report_context, ('source_context_records', 'source_taxon_occurrences', 'source_table_section')),
        (report_schemas, ('SourceMetric', 'ParsedDisease', 'ParsedTaxon', 'stable_id')),
    ):
        for name in names:
            monkeypatch.setattr(module, name, bomb)
    monkeypatch.setattr(report_parsing, '_TABLE_METRIC_DEFINITIONS', ())
    assert asdict(read(content)) == expected
    bomb.assert_not_called()


def test_import_and_native_execution_do_not_load_report_or_business_services():
    script = """
import tests.unit.conftest
import importlib.abc
import sys
class RejectDomain(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'modules.report' or fullname.startswith(('modules.report.', 'adapters.report.', 'db.')):
            raise AssertionError(fullname)
sys.meta_path.insert(0, RejectDomain())
from adapters.analysis.microbiome_source import read_microbiome_source_facts
from tests.unit._support.native_report_pdf import native_pdf
result=read_microbiome_source_facts(content=native_pdf(), text='', file_name='native.pdf', docmind=None)
assert len(result.observations) == 2
assert not any(n == 'modules.report' or n.startswith('modules.report.') for n in sys.modules)
"""
    result = subprocess.run([sys.executable, '-Xutf8', '-c', script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('revision', ['', None, 'document-microbiome-source-v2', 'latest', True])
def test_unknown_version_rejects_before_native_parsing(revision, monkeypatch):
    parse = Mock(side_effect=AssertionError('unknown version must not parse'))
    monkeypatch.setattr(parsing, '_extract_pdf', parse)
    with pytest.raises(ValueError, match='unsupported Document'):
        read(None, implementation_revision=revision)
    parse.assert_not_called()


@pytest.mark.parametrize('content', [None, b'', b'not a PDF', b'%PDF-1.7 malformed'])
def test_non_pdf_or_unreadable_original_keeps_optional_correction_behavior(content):
    assert read(content) is None


def test_source_only_pdf_never_enables_local_ocr(monkeypatch):
    class Page:
        def get_text(self, mode):
            return {} if mode == 'dict' else [] if mode == 'blocks' else ''

        def get_textpage_ocr(self, **kwargs):
            raise AssertionError('native source policy must not run OCR')

        def find_tables(self):
            return SimpleNamespace(tables=[])

    class Document:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def __iter__(self):
            return iter([Page()])

    monkeypatch.setattr(parsing.pymupdf, 'open', lambda **kwargs: Document())
    assert read(b'%PDF-1.7 scanned') is None


def test_frozen_risks_taxa_and_context_keep_actual_rows_separate_from_candidate_genes():
    document = parsing.ExtractedDocument(
        'panels.pdf',
        'pdf',
        '肠道菌群报告\n报告日期：2026-09-01\n===== PAGE 1 =====\n',
        tables=[
            parsing.ExtractedTable(
                1,
                [
                    ['疾病名称', '风险指数', '风险等级'],
                    ['结直肠癌', '0.20', '低风险'],
                    ['邻近说明不能当结果', '错误', '高风险'],
                ],
            ),
            parsing.ExtractedTable(
                1,
                [
                    ['拉丁学名', '中文名称', '检测结果(%)', '参考范围'],
                    ['Akkermansia muciniphila', '嗜粘蛋白-艾克曼菌', '4.565↑', '0-2.4003'],
                ],
                context='有益菌检出结果',
            ),
            parsing.ExtractedTable(
                1,
                [
                    ['名称', '检测结果', '检出毒力基因', '毒力基因列表'],
                    ['示例菌', '未检出', '-', 'geneA, geneB'],
                ],
            ),
            parsing.ExtractedTable(1, [['主诉：反复腹胀']], context='受检者信息'),
        ],
    )
    diseases = document_source.strict_report_source_diseases(document)
    assert [(r.name, r.source_score, r.source_level) for r in diseases] == [('结直肠癌', '0.20', '低风险')]
    taxa = source_context.source_taxon_occurrences(document)
    assert len(taxa) == 1 and taxa[0]['latinName'] == 'Akkermansia muciniphila'
    assert taxa[0]['value'] == '4.565' and taxa[0]['sourceStatus'] == '偏高'
    context = source_context.source_context_records(document)
    assert any(r['examResult'] == '反复腹胀' for r in context)
    genes = next(r for r in context if r['itemName'] == '示例菌')
    assert genes['detectedGenes'] == [] and genes['candidateGenes'] == ['geneA', 'geneB']


@pytest.mark.parametrize(
    'dates,expected',
    [
        ('报告日期：2026年9月1日\n报告日期：2026-09-01', '2026年9月1日'),
        ('报告日期：2026-02-30', None),
        ('报告日期：2026-09-01\n报告日期：2026-09-02', None),
        ('采样日期：2026-09-01', None),
    ],
)
def test_frozen_date_rules_preserve_ambiguity(dates, expected):
    assert parsing._explicit_report_date(parsing.ExtractedDocument('report.pdf', 'pdf', dates)) == expected


@pytest.mark.asyncio
async def test_actual_interpreter_cold_path_uses_fixed_native_policy_without_changing_model_input():
    content = native_pdf()
    text = '肠道菌群报告\n报告日期：2026-09-01\n肠道微生物健康指数 -0.02\n肠道菌群多样性 7.36'
    model = {
        'reportType': 'microbiome',
        'reportTime': '2026-09-01',
        'observations': [{'itemName': '肠道微生物健康指数', 'resultValue': '0.02'}],
    }
    interpreter, graph = interpreter_with(lambda _: model)
    interpreter._source_fact_reader = read_microbiome_source_facts
    result = await interpreter.analyze(
        text,
        source_snapshot={
            'content': content,
            'fileName': 'native.pdf',
            'contentSha256': hashlib.sha256(content).hexdigest(),
        },
    )
    assert interpreter.VERSION == 'analysis-health-report-v12'
    assert [call['input']['report_text'] for call in graph.calls] == [text]
    assert {(r['itemName'], r['resultValue']) for r in result['observations']} == {
        ('肠道菌群多样性', '7.36'),
        ('肠道微生物健康指数', '-0.02'),
    }
    assert result['processing']['status'] == 'complete'


def test_frozen_package_has_no_dynamic_report_imports():
    root = Path(parsing.__file__).parent
    for path in root.glob('*.py'):
        assert 'modules.report' not in path.read_text(encoding='utf-8'), path
    assert microbiome_source.parsing is parsing
