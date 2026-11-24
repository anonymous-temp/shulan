"""Demographic container judgments survive clinical section flattening."""

from copy import deepcopy

import pytest

from modules.analysis.application.analysis_outcomes import empty_analysis
from modules.analysis.application.extraction import merge_document_results
from tests.unit._support.current_document import report
from tests.unit.services.analysis.test_analysis_json import MemoryTrace
from tests.unit.services.analysis.test_basic_info_current import normalized
from tests.unit.services.analysis.test_basic_info_current import setup

FIRST = '片段甲\n受检者：P1\n受检者：P2\n指标甲：3\n'
SECOND = '片段乙\n受检者：P1\n姓名：张三\n性别：女\n年龄：42岁\n指标乙：4\n'
TEXT = FIRST + SECOND


def clinical(item_name, value, *, selected=False, date='2026-11-01'):
    raw = report(
        name='张三' if selected else None,
        gender='女' if selected else None,
        age=42 if selected else None,
        subject='P1',
    )
    raw.update(reportTime=date, observations=[{'itemName': item_name, 'resultValue': value}])
    return {**empty_analysis(), **normalized(raw, TEXT)}


def container(child, *, signal='ambiguous', metadata='absent'):
    raw = report(name=None, gender=None, age=None, subject='P1')
    raw.update(reportType='checkup', observations=[])
    for meta in raw['basicInfoMeta'].values():
        if signal == 'ambiguous':
            meta.update(
                status='unresolved',
                subjectStatus='ambiguous',
                reason='同一容器记录P1与P2，无法确认唯一受检者。',
                subjectEvidence={'quote': '受检者：P1\n受检者：P2'},
            )
        elif signal == 'unknown':
            meta.update(status='unresolved', subjectStatus='unknown')
    result = {**empty_analysis(), **normalized(raw, TEXT), 'sections': [child]}
    if signal == 'no_meta':
        result.pop('basicInfo')
        result.pop('basicInfoMeta')
    if metadata == 'empty':
        result['metadataEvidence'] = {}
    elif metadata == 'proved':
        result.update(title='体检报告', metadataEvidence={'title': {'quote': '体检报告'}})
    return result


def assert_ambiguous(result):
    assert result['basicInfo'] == dict.fromkeys(('name', 'gender', 'age'))
    assert all(meta['subjectStatus'] == 'ambiguous' for meta in result['basicInfoMeta'].values())
    assert all(meta['status'] in {'conflicting', 'unresolved'} for meta in result['basicInfoMeta'].values())
    assert {row['value'] for row in result['basicInfoMeta']['name']['candidates']} == {'张三'}


def assert_clinical_projection(result):
    assert [(row['itemName'], row['resultValue'], row['observedAt']) for row in result['observations']] in (
        [('指标甲', '3', '2026-11-01'), ('指标乙', '4', '2026-11-02')],
        [('指标乙', '4', '2026-11-02'), ('指标甲', '3', '2026-11-01')],
    )
    assert len(result['sections']) == 2
    assert sum(len(section['observations']) for section in result['sections']) == 2
    for row in result['observations']:
        source = result['sections'][row['sourceSection']]
        assert row['itemName'] in {item['itemName'] for item in source['observations']}


@pytest.mark.parametrize('metadata', ['absent', 'empty', 'proved'])
@pytest.mark.parametrize('reverse', [False, True])
def test_empty_ambiguous_container_does_not_depend_on_title_evidence(metadata, reverse):
    root = container(clinical('指标甲', '3'), metadata=metadata)
    peer = clinical('指标乙', '4', selected=True, date='2026-11-02')
    parts = [peer, root] if reverse else [root, peer]
    original = deepcopy(parts)

    result = merge_document_results(parts)

    assert_ambiguous(result)
    assert_clinical_projection(result)
    assert parts == original
    if metadata == 'proved':
        assert result['title'] == '体检报告'
        assert result['metadataEvidence']['title'] == {'quote': '体检报告'}


@pytest.mark.parametrize('wrappers', [1, 2])
@pytest.mark.parametrize('reverse', [False, True])
def test_nested_internal_container_keeps_its_empty_subject_judgment_without_clinical_sections(wrappers, reverse):
    root = container(clinical('指标甲', '3'))
    for _ in range(wrappers):
        root = container(root, signal='unknown')
    peer = clinical('指标乙', '4', selected=True, date='2026-11-02')
    parts = [peer, root] if reverse else [root, peer]
    original = deepcopy(parts)

    result = merge_document_results(parts)
    repeated = merge_document_results([result, empty_analysis()])

    assert_ambiguous(result)
    assert_clinical_projection(result)
    assert_ambiguous(repeated)
    assert [(row['itemName'], row['resultValue']) for row in repeated['observations']] == [
        (row['itemName'], row['resultValue']) for row in result['observations']
    ]
    assert parts == original


@pytest.mark.parametrize('signal', ['unknown', 'not_found', 'no_meta'])
def test_container_without_an_ambiguous_judgment_does_not_block_the_known_patient(signal):
    root = container(clinical('指标甲', '3'), signal=signal)
    peer = clinical('指标乙', '4', selected=True, date='2026-11-02')

    result = merge_document_results([root, peer])

    assert result['basicInfo'] == {'name': '张三', 'gender': '女', 'age': 42}
    assert all(meta['subjectStatus'] == 'single' for meta in result['basicInfoMeta'].values())
    assert_clinical_projection(result)


def test_matching_patient_on_a_container_keeps_the_existing_field_selection():
    root = container(clinical('指标甲', '3'))
    known = normalized(report(name='张三', subject='P1'), TEXT)
    root['basicInfo'], root['basicInfoMeta'] = known['basicInfo'], known['basicInfoMeta']
    peer = clinical('指标乙', '4', selected=True, date='2026-11-02')

    result = merge_document_results([root, peer])

    assert result['basicInfo'] == {'name': '张三', 'gender': '女', 'age': 42}
    assert all(meta['status'] == 'extracted' for meta in result['basicInfoMeta'].values())
    assert len(result['observations']) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('reverse', [False, True])
async def test_real_partitioning_and_checkpoint_reuse_keep_an_ambiguous_container(reverse):
    def respond(prompt):
        if '片段甲' not in prompt:
            raw = report(name='张三', subject='P1')
            raw['observations'] = [{'itemName': '指标乙', 'resultValue': '4'}]
            return raw
        raw = report(name=None, gender=None, age=None)
        raw.update(reportType='checkup', observations=[])
        for meta in raw['basicInfoMeta'].values():
            meta.update(status='unresolved', subjectStatus='ambiguous', reason='原文有P1、P2两位受检者。')
        child = report(name=None, gender=None, age=None)
        child['observations'] = [{'itemName': '指标甲', 'resultValue': '3'}]
        raw['sections'] = [child]
        return raw

    current, _, graph = setup(respond)
    width = max(len(FIRST), len(SECOND)) + 2
    text = ''.join(part.ljust(width - 2) + '\n\n' for part in ((SECOND, FIRST) if reverse else (FIRST, SECOND)))
    current.MAX_ANALYZE_INPUT_TOKENS = width
    trace = MemoryTrace()

    result = await current.analyze(text, trace=trace)
    repeated = await current.analyze(text, trace=trace)

    assert_ambiguous(result)
    assert result['processing']['status'] == 'complete'
    assert {(row['itemName'], row['resultValue']) for row in result['observations']} == {
        ('指标甲', '3'),
        ('指标乙', '4'),
    }
    assert len(result['sections']) == 2
    assert repeated['basicInfoMeta'] == result['basicInfoMeta']
    assert repeated['observations'] == result['observations']
    assert len(graph.calls) == 3
    saved = trace.payloads[f'analysis_part_{1 if reverse else 0:05d}']['rawData']
    assert all(meta['subjectStatus'] == 'ambiguous' for meta in saved['basicInfoMeta'].values())
    assert all(meta['reason'] == '原文有P1、P2两位受检者。' for meta in saved['basicInfoMeta'].values())
