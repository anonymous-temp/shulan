"""Report v16 projection from the current fixed Document archive."""

from collections import Counter
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from unittest.mock import Mock

import pymupdf
import pytest

from adapters.analysis.materials import create_document_material_reader
from modules.analysis.application.analysis_outcomes import empty_analysis
from modules.analysis.application.extraction import merge_document_results
from modules.analysis.snapshots import DocumentScope
from modules.report import parsing
from modules.report.document_projection import REPORT_SOURCE_SELECTION
from modules.report.document_projection import ReportSourceInput
from modules.report.document_projection import (
    project_current_report_source_with_observation_refs as project_current_report_source,
)
from tests.unit._support.current_document import REPORT_TEXT
from tests.unit._support.current_document import UPLOAD
from tests.unit._support.current_document import actual_analysis_preparation
from tests.unit._support.current_document import capture
from tests.unit._support.current_document import environment
from tests.unit._support.current_document import model_report
from tests.unit._support.current_document import no_value
from tests.unit._support.native_report_pdf import native_pdf
from utils.report_source_observations import report_source_items


async def archive(*, scope=None, status='complete', edit=None, content=b'synthetic report', clinical_text=''):
    options = {} if scope is None else {'scope': scope}
    env = await environment(status=status, edit=edit, **options)
    env.document.update(contentSha256=sha256(content).hexdigest(), byteSize=len(content))
    text = env.parsed['text'] + clinical_text
    env.parsed.update(success=True, text=text, pages=[{'text': text}])
    env.bundle = await capture(env)
    return env, content


async def prepare_original(model, *, text=REPORT_TEXT, native_content=None):
    flow = await actual_analysis_preparation([model], text=text, native_content=native_content)
    bundle = await flow.prepared.prepare_bundle(scope=flow.scope, **UPLOAD)
    reader = create_document_material_reader(
        snapshots=flow.snapshots,
        snapshot_chunks=flow.snapshot_chunks,
        traces=flow.traces,
        trace_chunks=flow.trace_chunks,
    )
    archived = await reader.read_original(scope=flow.scope, material_snapshot_ref=bundle.material.snapshot_id)
    assert bundle.facts.payload['quality']['status'] == 'complete'
    return flow, bundle, archived


def measurement(name, value, **fields):
    return {'itemName': name, 'resultValue': value, 'kind': 'measurement', **fields}


def section(report_type, report_time, *, observations=(), expansion=None):
    return dict(
        empty_analysis(),
        reportType=report_type,
        reportTime=report_time,
        observations=list(observations),
        expansion=expansion,
    )


def result_table(headers, *rows):
    def line(values):
        return '| ' + ' | '.join(values) + ' |\n'

    return '\n' + line(headers) + line(['---'] * len(headers)) + ''.join(map(line, rows))


def lab_round(basic, *, with_demographics):
    labs = [
        dict(
            empty_analysis(),
            reportType='lab',
            status='normal',
            reportTime='2026-11-01',
            observations=[{'itemName': label, 'resultValue': '12', 'unit': 'U/L'}],
        )
        for label in ('ALT', 'AST')
    ]
    demographics = dict(empty_analysis(), reportType='checkup', sections=labs[:1])
    demographics.update({key: deepcopy(basic[key]) for key in ('basicInfo', 'basicInfoMeta')})
    for field in ('name', 'gender', 'age'):
        if field != 'age' or not with_demographics:
            no_value(demographics, field)
    labs = [demographics, labs[1]]
    return dict(merge_document_results(labs), processing={'status': 'complete'})


@pytest.mark.asyncio
async def test_derived_root_mirrors_do_not_change_report_clinical_provenance():
    text = result_table(
        ('检验项目', '结果', '单位', '检验日期'),
        ('ALT', '12', 'U/L', '2026-11-01'),
        ('AST', '12', 'U/L', '2026-11-01'),
    )
    env, content = await archive(clinical_text=text)
    basic = env.raw
    env.raw = lab_round(basic, with_demographics=False)
    first = await capture(env)
    env.raw = lab_round(basic, with_demographics=True)
    env.document['interpretationResultRound'] = 2
    second = await capture(env)
    assert first.facts.payload['clinicalContentDigest'] == second.facts.payload['clinicalContentDigest']
    assert first.facts.payload['basicInfo'] != second.facts.payload['basicInfo']
    original = second.understanding.payload
    assert original['interpretation']['reportType'] == 'checkup'
    assert original['interpretation']['observations']
    assert first.understanding.snapshot_id != second.understanding.snapshot_id

    _, before = await project_current_report_source(((first, content),))
    _, after = await project_current_report_source(((second, content),))

    def clinical_rows(projection):
        return [
            {key: value for key, value in row.items() if key != 'analysisObservationRefs'}
            for row in projection['supplementaryHospitalData']
        ]

    assert clinical_rows(before) == clinical_rows(after)
    for bundle, projection in ((first, before), (second, after)):
        refs = {row['label']: row['observationRef'] for row in bundle.facts.payload['observations']}
        for row in projection['supplementaryHospitalData']:
            assert row['analysisObservationRefs'] == [refs[row['itemName']]]
    assert [
        (row['itemName'], row['sourceReportType'], row['reportTime']) for row in after['supplementaryHospitalData']
    ] == [('ALT', 'lab', '2026-11-01'), ('AST', 'lab', '2026-11-01')]
    assert second.understanding.payload == original


@pytest.mark.asyncio
@pytest.mark.parametrize('report_date', ['2026-11-13', '2026/11/13'])
async def test_current_projection_uses_child_lab_provenance_for_a_dated_mirror(report_date):
    model = model_report()
    child = section('lab', report_date, observations=[measurement('ALT', '12', unit='U/L')])
    model.update(reportType='microbiome', reportTime=None, observations=[], sections=[child])
    text = REPORT_TEXT.replace('ALT 12 U/L\n', '')
    text += result_table(('检验项目', '结果', '单位', '检验日期'), ('ALT', '12', 'U/L', report_date))
    _, bundle, original = await prepare_original(model, text=text)
    fixed = bundle.understanding.payload['interpretation']
    assert fixed['observations'][0]['sourceSection'] == 0
    assert fixed['sections'][0]['reportTime'] == report_date

    _, projection = await project_current_report_source(((bundle, original),))

    alt = [row for row in projection['supplementaryHospitalData'] if row['itemName'] == 'ALT']
    expected = {'/sections/0/observations/0'}
    if '/' in report_date:
        expected.add('/observations/0')
    fixed_refs = {row['observationRef'] for row in bundle.facts.payload['observations']}
    assert fixed_refs == expected
    (row,) = alt
    assert set(row['analysisObservationRefs']) == expected
    assert len(row['analysisObservationRefs']) == len(expected)
    assert [(row['sourceReportType'], row['reportTime'], row['testResultValue']) for row in alt] == [
        ('lab', report_date, '12 U/L')
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize('aggregate', ['direct', 'deduplicated', 'with_root_evidence'])
async def test_current_projection_keeps_dated_imaging_occurrences_and_requires_field_specific_root_proof(aggregate):
    finding, diagnosis = 'Synthetic identical finding', 'Synthetic identical diagnosis'
    dates = ('2026-11-01', '2026-11-02')
    root_date = '2026-11-03'
    model = model_report()
    model.update(
        reportType='imaging',
        reportTime=root_date if aggregate == 'with_root_evidence' else None,
        observations=[],
        expansion={
            'findings': None if aggregate == 'direct' else finding,
            'diagnosis': None if aggregate == 'direct' else diagnosis,
        },
        sections=[section('imaging', date, expansion={'findings': finding, 'diagnosis': diagnosis}) for date in dates],
    )
    text = REPORT_TEXT + '\n'.join(f'报告日期：{date}\n影像所见：{finding}\n影像诊断：{diagnosis}\n' for date in dates)
    if aggregate == 'with_root_evidence':
        text += f'报告日期：{root_date}\n影像所见：{finding}\n健康建议：{diagnosis}\n'
    _, bundle, original = await prepare_original(model, text=text)
    interpretation = bundle.understanding.payload['interpretation']
    expected_root = '\n'.join((finding, finding)) if aggregate == 'direct' else finding
    assert interpretation['expansion']['findings'] == expected_root

    _, projection = await project_current_report_source(((bundle, original),))

    clinical = projection['supplementaryHospitalData']
    findings = Counter(
        (row['reportTime'], row['examResult']) for row in clinical if row['itemName'].endswith('影像所见')
    )
    diagnoses = Counter(
        (row['reportTime'], row['examResult']) for row in clinical if row['itemName'].endswith('影像诊断')
    )
    expected_dates = (*dates, root_date) if aggregate == 'with_root_evidence' else dates
    assert findings == Counter((date, finding) for date in expected_dates)
    assert diagnoses == Counter((date, diagnosis) for date in dates)
    for row in clinical:
        if row['reportTime'] in dates and row['examResult'] in {finding, diagnosis}:
            field = 'findings' if row['examResult'] == finding else 'diagnosis'
            assert row['analysisObservationRefs'] == [f'/sections/{dates.index(row["reportTime"])}/expansion/{field}']


@pytest.mark.asyncio
async def test_fixed_clinical_paths_keep_independent_root_observation():
    def interpretation(raw):
        imaging = [
            section('imaging', date, observations=[measurement('Child measure', '12')])
            for date in ('2026-11-01', '2026-11-02')
        ]
        merged = merge_document_results(imaging)
        merged['observations'].append(measurement('Root measure', '14', observedAt='2026-11-03'))
        raw.update(merged)

    text = result_table(
        ('检验项目', '结果', '检验日期'),
        ('Child measure', '12', '2026-11-01'),
        ('Child measure', '12', '2026-11-02'),
        ('Root measure', '14', '2026-11-03'),
    )
    env, content = await archive(edit=interpretation, clinical_text=text)
    refs = {row['observationRef'] for row in env.bundle.facts.payload['observations']}
    assert '/observations/2' in refs
    assert '/observations/0' not in refs

    _, projection = await project_current_report_source(((env.bundle, content),))

    rows = projection['supplementaryHospitalData']
    assert [(row['reportTime'], row['testResultValue']) for row in rows if row['itemName'] == 'Root measure'] == [
        ('2026-11-03', '14')
    ]
    assert [(row['reportTime'], row['testResultValue']) for row in rows if row['itemName'] == 'Child measure'] == [
        ('2026-11-01', '12'),
        ('2026-11-02', '12'),
    ]


@pytest.mark.asyncio
async def test_current_projection_distinguishes_same_date_value_and_merges_one_original_occurrence():
    date = '2026-11-12'
    stool = measurement('pH', '7.1', specimen='粪便', method='试纸法')
    urine = measurement('pH', '7.1', specimen='尿液', method='电极法')
    model = model_report()
    model.update(
        reportType='checkup',
        observations=[],
        sections=[
            section('microbiome', date, observations=[stool]),
            section('lab', date, observations=[urine]),
            section('microbiome', date, observations=[stool]),
        ],
    )
    text = REPORT_TEXT + result_table(
        ('检测项目', '样本含量', '标本', '方法', '参考范围'),
        ('pH', '7.1', '粪便', '试纸法', '6.5–7.5'),
        ('pH', '7.1', '尿液', '电极法', '6.5–7.5'),
    )
    _, bundle, original = await prepare_original(model, text=text)
    assert [row['specimen'] for row in bundle.facts.payload['observations'] if row['label'] == 'pH'] == [
        '粪便',
        '尿液',
        '粪便',
    ]

    _, projection = await project_current_report_source(((bundle, original),))

    rows = [row for row in projection['supplementaryHospitalData'] if row['itemName'] == 'pH']
    assert {
        (row['sourceReportType'], row['reportTime'], row['testResultValue'], row['specimen'], row['method'])
        for row in rows
    } == {
        ('microbiome', date, '7.1', '粪便', '试纸法'),
        ('lab', date, '7.1', '尿液', '电极法'),
    }
    assert len(rows) == 2
    assert len({row['sourceOccurrence'] for row in rows}) == 2
    assert {row['specimen']: row['analysisObservationRefs'] for row in rows} == {
        stool['specimen']: ['/sections/0/observations/0', '/sections/2/observations/0'],
        urine['specimen']: ['/sections/1/observations/0'],
    }


@pytest.mark.asyncio
async def test_current_projection_keeps_distinct_original_table_occurrences_without_model_rows():
    date = '2026-11-12'
    text = REPORT_TEXT + result_table(
        ('检测项目', '检测结果', '标本', '方法', '检测日期'),
        ('pH', '7.1', '粪便', '试纸法', date),
        ('pH', '7.1', '尿液', '电极法', date),
    )
    _, bundle, original = await prepare_original(model_report(), text=text)
    assert all(row['label'] != 'pH' for row in bundle.facts.payload['observations'])

    _, projection = await project_current_report_source(((bundle, original),))

    rows = [row for row in projection['supplementaryHospitalData'] if row['itemName'] == 'pH']
    assert Counter((row['reportTime'], row['testResultValue'], row['specimen'], row['method']) for row in rows) == {
        (date, '7.1', '粪便', '试纸法'): 1,
        (date, '7.1', '尿液', '电极法'): 1,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize('contradiction', [{'specimen': '粪便'}, {'observedAt': '2026-11-13'}])
async def test_current_projection_keeps_original_when_model_metadata_conflicts(contradiction):
    date = '2026-11-12'
    model = model_report()
    model['sections'] = [section('imaging', date, observations=[measurement('pH', '7.1', **contradiction)])]
    text = REPORT_TEXT + result_table(
        ('检测项目', '检测结果', '标本', '方法', '检测日期'),
        ('pH', '7.1', '尿液', '电极法', date),
    )
    _, bundle, original = await prepare_original(model, text=text)

    _, projection = await project_current_report_source(((bundle, original),))

    rows = [row for row in projection['supplementaryHospitalData'] if row['itemName'] == 'pH']
    assert [(row['reportTime'], row['testResultValue'], row['specimen'], row['method']) for row in rows] == [
        (date, '7.1', '尿液', '电极法')
    ]
    assert rows[0].get('sourceReportType') != 'imaging'


def native_lab_pdf(*, result_count=1, independent_line=False):
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((40, 40), '1. 检验报告', fontname='china-s', fontsize=14)
        rows = [('检验项目', '结果', '单位', '参考范围'), *[('ALT', '12', 'U/L', '0–40')] * result_count]
        if independent_line:
            page.insert_text((40, 70), 'ALT 12 U/L', fontsize=11)
        for index, row in enumerate(rows):
            for x, value in zip((40, 185, 295, 410), row, strict=True):
                page.insert_text((x, 100 + index * 25), value, fontname='china-s', fontsize=11)
        page.insert_text((40, 110 + len(rows) * 25), '检查时间：2026-11-12', fontname='china-s', fontsize=11)
        return pdf.tobytes()


@pytest.mark.asyncio
async def test_current_projection_uses_native_result_cells_when_the_archived_parser_disagrees(monkeypatch):
    content = native_lab_pdf()
    model = model_report()
    model['observations'] = [measurement('ALT', '40', unit='U/L')]
    text = result_table(('检验项目', '结果', '单位', '参考范围'), ('ALT', '40', 'U/L', '0–40'))
    text = REPORT_TEXT.replace('ALT 12 U/L\n', '') + text
    _, bundle, original = await prepare_original(model, text=text, native_content=content)
    assert original == content
    extract = Mock(wraps=parsing._extract_pdf)
    monkeypatch.setattr(parsing, '_extract_pdf', extract)

    _, projection = await project_current_report_source(((bundle, original),))

    extract.assert_called_once_with('synthetic.pdf', original, ocr=False, prove_result_rows=True)
    rows = [row for row in projection['supplementaryHospitalData'] if row['itemName'] == 'ALT']
    assert {(row['reportTime'], row['testResultValue']) for row in rows} == {('2026-11-12', '12 U/L')}
    candidates = [row for _, row in report_source_items(projection) if row['itemName'] == 'ALT']
    assert {str(row['resultValue']) for row in candidates} == {'12'}


@pytest.mark.asyncio
@pytest.mark.parametrize('occurrences', [1, 2])
async def test_current_projection_preserves_distinct_original_inline_results(occurrences):
    text = REPORT_TEXT
    if occurrences == 2:
        demographics = REPORT_TEXT.replace('ALT 12 U/L\n', '')
        text = ''.join(
            f'\n===== PAGE {page} =====\n{demographics}ALT: 12 U/L 0-40\n' for page in range(1, occurrences + 1)
        )
    _, bundle, original = await prepare_original(model_report(), text=text)

    _, projection = await project_current_report_source(((bundle, original),))

    rows = [row for row in projection['supplementaryHospitalData'] if row['itemName'] == 'ALT']
    assert [(row['reportTime'], row['testResultValue']) for row in rows] == [('2026-11-12', '12 U/L')] * occurrences
    assert len({row['sourceOccurrence'] for row in rows}) == occurrences
    if occurrences == 1:
        assert rows[0]['analysisObservationRefs'] == ['/observations/0']
    else:
        assert all('analysisObservationRefs' not in row for row in rows)


@pytest.mark.asyncio
async def test_current_projection_prefers_native_metadata_when_result_values_agree():
    text = REPORT_TEXT + result_table(('检验项目', '结果', '单位', '参考范围'), ('ALT', '12', 'U/L', '50–90'))
    _, bundle, original = await prepare_original(model_report(), text=text, native_content=native_lab_pdf())

    _, projection = await project_current_report_source(((bundle, original),))

    rows = [row for row in projection['supplementaryHospitalData'] if row['itemName'] == 'ALT']
    assert len(rows) == 1
    assert rows[0]['testResultValue'] == '12 U/L'
    assert rows[0]['referenceRange'] == '0–40'
    assert rows[0]['sourceOccurrence'].startswith('native:')
    assert rows[0]['analysisObservationRefs'] == ['/observations/0']


@pytest.mark.asyncio
async def test_observation_refs_are_omitted_for_independent_native_inline_and_table_cells():
    content = native_lab_pdf(independent_line=True)
    text = REPORT_TEXT + result_table(('检验项目', '结果', '单位', '参考范围'), ('ALT', '12', 'U/L', '0–40'))
    _, bundle, original = await prepare_original(model_report(), text=text, native_content=content)

    _, projection = await project_current_report_source(((bundle, original),))

    rows = [row for row in projection['supplementaryHospitalData'] if row['itemName'] == 'ALT']
    assert len(rows) == len({row['sourceOccurrence'] for row in rows}) == 2
    assert any(':line:' in row['sourceOccurrence'] for row in rows)
    assert any(':table:' in row['sourceOccurrence'] for row in rows)
    assert all('analysisObservationRefs' not in row for row in rows)


@pytest.mark.asyncio
async def test_current_projection_retains_two_native_occurrences_when_ocr_repeats_both():
    headers = ('检验项目', '结果', '单位', '参考范围')
    result = ('ALT', '12', 'U/L', '0–40')
    text = REPORT_TEXT + result_table(headers, result, result)
    content = native_lab_pdf(result_count=2)
    _, bundle, original = await prepare_original(model_report(), text=text, native_content=content)

    _, projection = await project_current_report_source(((bundle, original),))

    rows = [row for row in projection['supplementaryHospitalData'] if row['itemName'] == 'ALT']
    assert [(row['reportTime'], row['testResultValue']) for row in rows] == [('2026-11-12', '12 U/L')] * 2
    assert len({row['sourceOccurrence'] for row in rows}) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('title', ['健康 建议', '4. 健康 建议', '四、健康指导', '4.2 注意事项'])
async def test_current_projection_excludes_advice_sections_even_after_their_dates(title):
    model = model_report()
    model['sections'] = [
        section('microbiome', '2026-11-12', observations=[measurement('肠道菌群多样性', '9.99')]),
    ]
    text = REPORT_TEXT + result_table(('检验项目', '结果', '单位', '参考范围'), ('ALT', '12', 'U/L', '0–40'))
    text += f'\n{title}\n肠道菌群多样性 9.99\n报告日期：2026-11-12\n'
    text += result_table(('检测项目', '检测结果', '参考范围'), ('肠道菌群多样性', '9.99', '0–10'))
    _, bundle, original = await prepare_original(model, text=text)
    assert any(row['label'] == '肠道菌群多样性' for row in bundle.facts.payload['observations'])

    _, projection = await project_current_report_source(((bundle, original),))

    assert all(str(row['value']) != '9.99' for row in projection['metrics'])
    candidates = [row for _, row in report_source_items(projection)]
    assert {row['itemName'] for row in candidates} == {'ALT'}
    assert all(str(row['resultValue']) != '9.99' for row in candidates)


@pytest.mark.asyncio
async def test_current_projection_only_admits_observations_supported_by_the_original_result_role():
    printed, fabricated = 'Patient conclusion printed', 'Made up conclusion'
    advice = 'Advice only, no patient finding'
    model = model_report()
    model['observations'] += [
        measurement('ALT', '40', unit='U/L'),
        measurement('Unprinted measurement', '666'),
        {'itemName': 'Synthetic assessment', 'resultValue': printed, 'kind': 'assessment'},
        {'itemName': 'Fabricated assessment', 'resultValue': fabricated, 'kind': 'assessment'},
        {'itemName': 'Advice only', 'resultValue': advice, 'kind': 'assessment'},
    ]
    model['sections'] = [
        section('imaging', '2026-11-12', expansion={'findings': fabricated, 'diagnosis': advice}),
        section('microbiome', '2026-11-12', observations=[measurement('肠道菌群多样性', '9.99')]),
    ]
    text = REPORT_TEXT + result_table(('检验项目', '结果', '单位', '参考范围'), ('ALT', '12', 'U/L', '40'))
    text += f'\n检查结论\n1. Synthetic assessment: {printed}\n健康建议\nAdvice only: {advice}\n'
    _, bundle, original = await prepare_original(model, text=text)
    assert any(row['rawValue'] == fabricated for row in bundle.facts.payload['observations'])

    _, projection = await project_current_report_source(((bundle, original),))

    rows = projection['supplementaryHospitalData']
    assert any(row['itemName'] == 'Synthetic assessment' and printed in row['examResult'] for row in rows)
    assert {(row['itemName'], row['testResultValue']) for row in rows if row['itemName'] == 'ALT'} == {
        ('ALT', '12 U/L')
    }
    candidates = [row for _, row in report_source_items(projection)]
    assert not {row['itemName'] for row in candidates}.intersection(
        {'Fabricated assessment', 'Unprinted measurement', 'Advice only', '肠道菌群多样性'}
    )
    assert not {str(row['resultValue']) for row in candidates}.intersection({fabricated, advice, '40', '666', '9.99'})


@pytest.mark.asyncio
@pytest.mark.parametrize('distinct_context', [False, True])
async def test_observation_refs_distinguish_inline_from_independent_table(distinct_context):
    model = model_report()
    if distinct_context:
        model['observations'][0]['observedAt'] = '2026-11-13'
    table_date = '2026-11-13' if distinct_context else '2026-11-12'
    text = REPORT_TEXT + f'\n报告日期：{table_date}\n'
    text += result_table(('检验项目', '结果', '单位', '检验日期'), ('ALT', '12', 'U/L', table_date))
    _, bundle, original = await prepare_original(model, text=text)

    _, projection = await project_current_report_source(((bundle, original),))

    rows = [row for row in projection['supplementaryHospitalData'] if row['itemName'] == 'ALT']
    assert len(rows) == len({row['sourceOccurrence'] for row in rows}) == 2
    expected = ['/observations/0'] if distinct_context else None
    assert [row.get('analysisObservationRefs') for row in rows if ':table:' in row['sourceOccurrence']] == [expected]
    assert all('analysisObservationRefs' not in row for row in rows if ':line:' in row['sourceOccurrence'])


@pytest.mark.asyncio
async def test_observation_refs_distinguish_independent_assessment_prose_and_table():
    model = model_report()
    model['observations'] = [
        {'itemName': 'Synthetic risk', 'resultValue': '12', 'kind': 'assessment'},
    ]
    text = REPORT_TEXT.replace('ALT 12 U/L\n', '') + '检查结论\n1. Synthetic risk: 12\n'
    text += result_table(('检测项目', '评估值'), ('Synthetic risk', '12'))
    _, bundle, original = await prepare_original(model, text=text)

    _, projection = await project_current_report_source(((bundle, original),))

    rows = [row for row in projection['supplementaryHospitalData'] if row['itemName'] == 'Synthetic risk']
    assert len(rows) == len({row['sourceOccurrence'] for row in rows}) == 2
    assert all('analysisObservationRefs' not in row for row in rows)


def native_assessment_pdf():
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        for x, y, value in (
            (45, 30, '报告日期：2026-11-12'),
            (45, 60, '检查结论'),
            (50, 150, '项目'),
            (240, 150, '检查结论'),
        ):
            page.insert_text((x, y), value, fontname='china-s')
        finding = 'Synthetic assessment: Printed finding'
        page.insert_text((45, 85), '1. ' + finding)
        for x in (40, 220, 500):
            page.draw_line((x, 130), (x, 190))
        for y in (130, 160, 190):
            page.draw_line((40, y), (500, y))
        page.insert_text((50, 180), 'Synthetic assessment')
        page.insert_text((240, 180), finding)
        return pdf.tobytes()


@pytest.mark.asyncio
@pytest.mark.parametrize('pdf', [False, True])
async def test_observation_refs_distinguish_assessment_prose_and_conclusion_cell(pdf):
    model = model_report()
    model.update(
        reportType='unknown',
        observations=[
            {'itemName': 'Synthetic assessment', 'resultValue': 'Printed finding', 'kind': 'assessment'},
        ],
    )
    text = REPORT_TEXT.replace('ALT 12 U/L\n', '') + '检查结论\n1. Synthetic assessment: Printed finding\n\n'
    text += result_table(('项目', '检查结论'), ('Synthetic assessment', 'Synthetic assessment: Printed finding'))
    _, bundle, original = await prepare_original(
        model, text=text, native_content=native_assessment_pdf() if pdf else None
    )

    _, projection = await project_current_report_source(((bundle, original),))

    rows = [row for row in projection['supplementaryHospitalData'] if row['itemName'] == 'Synthetic assessment']
    assert len(rows) == len({row['sourceOccurrence'] for row in rows}) == 2
    assert all('analysisObservationRefs' not in row for row in rows)
    if pdf:
        assert any(row['sourceOccurrence'].startswith('native:conclusion:') for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize('printed_count', [0, 1, 2])
async def test_fallback_root_assessment_keeps_only_original_verified_refs(printed_count):
    model = model_report()
    model.update(
        reportType='unknown',
        observations=[{'itemName': 'Synthetic assessment', 'resultValue': 'Printed finding', 'kind': 'assessment'}],
    )
    text = REPORT_TEXT.replace('ALT 12 U/L\n', '')
    for page in range(1, printed_count + 1):
        text += f'\n===== PAGE {page} =====\n检查结论\n1. Synthetic assessment: Printed finding\n'
    _, bundle, original = await prepare_original(model, text=text)
    assert bundle.facts.payload['observations'][0]['observationRef'] == '/observations/0'

    _, projection = await project_current_report_source(((bundle, original),))

    rows = [row for row in projection['supplementaryHospitalData'] if row['itemName'] == 'Synthetic assessment']
    assert len(rows) == len({row['sourceOccurrence'] for row in rows}) == printed_count
    assert all(row['dataType'] == 'EXAM' and row.get('sourceReportType') != 'unknown' for row in rows)
    assert [row.get('analysisObservationRefs') for row in rows] == (
        [['/observations/0']] if printed_count == 1 else [None] * printed_count
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['findings', 'diagnosis'])
async def test_root_imaging_exact_join_can_have_its_own_original_occurrence(field):
    model = model_report()
    labels = {'findings': '影像所见', 'diagnosis': '影像诊断'}
    label = labels[field]
    child_text = 'Synthetic finding repeated in two children'
    combined = '\n'.join((child_text, child_text))
    dates = ('2026-11-12', '2026-11-13', '2026-11-14')
    model.update(
        reportType='imaging',
        reportTime=dates[2],
        observations=[],
        expansion={field: combined},
        sections=[section('imaging', date, expansion={field: child_text}) for date in dates[:2]],
    )
    text = REPORT_TEXT + '\n'.join(f'报告日期：{date}\n{label}：{child_text}\n' for date in dates[:2])
    text += f'报告日期：{dates[2]}\n{label}：{combined}\n'
    _, bundle, original = await prepare_original(model, text=text)

    assert {row['observationRef'] for row in bundle.facts.payload['observations']} == {
        f'/expansion/{field}',
        f'/sections/0/expansion/{field}',
        f'/sections/1/expansion/{field}',
    }
    _, projection = await project_current_report_source(((bundle, original),))

    rows = [row for row in projection['supplementaryHospitalData'] if row['itemName'].endswith(label)]
    assert len(rows) == len({row['sourceOccurrence'] for row in rows}) == 3
    assert {row['reportTime']: row['analysisObservationRefs'] for row in rows} == {
        dates[0]: [f'/sections/0/expansion/{field}'],
        dates[1]: [f'/sections/1/expansion/{field}'],
        dates[2]: [f'/expansion/{field}'],
    }


@pytest.mark.asyncio
async def test_real_archive_supplies_native_pdf_geometry_without_a_second_ocr(monkeypatch):
    content = native_pdf()
    model = model_report()
    model['reportType'] = 'microbiome'
    flow, bundle, archived = await prepare_original(model, native_content=content)
    assert archived == content
    extract = Mock(wraps=parsing._extract_pdf)
    monkeypatch.setattr(parsing, '_extract_pdf', extract)
    source = ReportSourceInput.from_current_bundle(bundle, archived)

    selection, projection = await project_current_report_source(((bundle, archived),))

    assert selection == REPORT_SOURCE_SELECTION
    extract.assert_called_once_with(source.file_name, archived, ocr=False, prove_result_rows=True)
    manifest = source.manifest
    assert manifest['mode'] == 'current'
    assert manifest['scope'] == flow.scope.as_dict()
    assert manifest['inputSelection'] == bundle.understanding.payload['selection']
    assert manifest['materialSnapshotRef'] == bundle.material.snapshot_id
    assert manifest['factsSnapshotRef'] == bundle.facts.snapshot_id
    assert manifest['understandingSnapshotRef'] == bundle.understanding.snapshot_id
    assert manifest['interpretationRound'] == bundle.facts.payload['interpretationRound']
    assert manifest['clinicalContentDigest'] == bundle.facts.payload['clinicalContentDigest']
    assert manifest['quality'] == bundle.facts.payload['quality']
    assert manifest['clinicalAccepted'] is True
    assert projection['sourceChecksum'] == sha256(archived).hexdigest()
    values = {row['code']: row['value'] for row in projection['metrics']}
    assert values['diversity'] == '7.36'
    assert values['gmhi'] == '-0.02'
    assert len(flow.gateway.complete_requests) == len(flow.upstream.client.submit_calls) == 1


@pytest.mark.asyncio
async def test_receipt_local_duplicates_extract_once_and_conflicting_rounds_fail_before_extraction(monkeypatch):
    env, content = await archive()
    bundle = replace(env.bundle, receipt=env.original.receipt)
    replay = replace(bundle, receipt=replace(bundle.receipt, trace_id='new-upload-request', reused=True))
    first = ReportSourceInput.from_current_bundle(bundle, content)
    second = ReportSourceInput.from_current_bundle(replay, content)
    assert first == second
    extract = Mock(wraps=parsing._extract_pdf)
    monkeypatch.setattr(parsing, '_extract_pdf', extract)

    _, projection = await project_current_report_source(((bundle, content), (replay, content)))

    extract.assert_called_once_with(first.file_name, content, ocr=False, prove_result_rows=True)
    assert len(projection['sourceDocuments']) == 1
    extract.reset_mock()
    env.document['interpretationResultRound'] = 2
    conflicting = await capture(env)
    with pytest.raises(ValueError, match='conflict'):
        await project_current_report_source(((bundle, content), (conflicting, content)))
    extract.assert_not_called()


@pytest.mark.asyncio
async def test_archive_binding_rejects_wrong_bytes_size_and_cross_scope_before_extraction(monkeypatch):
    env, content = await archive()
    other, _ = await archive(scope=DocumentScope(env.scope.user_id, 'other-subject'))
    extract = Mock(wraps=parsing._extract_pdf)
    monkeypatch.setattr(parsing, '_extract_pdf', extract)
    with pytest.raises(ValueError, match='bytes|digest|checksum'):
        await project_current_report_source(((env.bundle, b'different report'),))
    with pytest.raises(PermissionError):
        await project_current_report_source(((env.bundle, content), (other.bundle, content)))
    env.document.update(byteSize=len(content) + 1, interpretationResultRound=2)
    wrong_size = await capture(env)
    with pytest.raises(ValueError, match='size'):
        ReportSourceInput.from_current_bundle(wrong_size, content)
    extract.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['partial', 'unavailable', None])
async def test_projection_preserves_current_document_quality(status):
    def processing(raw):
        if status is None:
            raw.pop('processing')
        else:
            raw['processing'].update(retryable=False, issueCount=2)

    env, content = await archive(status=status, edit=processing)
    source = ReportSourceInput.from_current_bundle(env.bundle, content)

    _, projection = await project_current_report_source(((env.bundle, content),))

    assert source.manifest['quality'] == env.bundle.facts.payload['quality']
    assert projection['processing']['status'] == (status or 'unavailable')
    if status is not None:
        assert projection['processing']['retryable'] is False
        assert projection['processing']['issueCount'] == 2


@pytest.mark.asyncio
async def test_later_result_table_reopens_without_accepting_advice_or_reference_values():
    model = model_report()
    model['observations'] += [
        measurement('AST', '53', unit='U/L'),
        measurement('AST', '40', unit='U/L'),
        measurement('GGT', '88', unit='U/L'),
    ]
    text = REPORT_TEXT.replace('ALT 12 U/L\n', '')
    text += '\n检查日期：2026-11-12\n建议\nGGT 88 U/L\n'
    text += result_table(('项目名称', '检查结果', '参考值', '单位'), ('AST', '53', '0-40', 'U/L'))
    text += '\nGGT 88 U/L\n'
    _, bundle, original = await prepare_original(model, text=text)

    _, projection = await project_current_report_source(((bundle, original),))

    rows = projection['supplementaryHospitalData']
    assert len(rows) == 1
    assert rows[0]['itemName'] == 'AST'
    assert rows[0]['testResultValue'] == '53 U/L'
    assert rows[0]['reportTime'] == '2026-11-12'
    assert rows[0]['analysisObservationRefs'] == ['/observations/1']
