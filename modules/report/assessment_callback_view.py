"""Project the reviewed six-chapter assessment into PDF reading order."""

from collections.abc import Mapping
from typing import Any

ASSESSMENT_SECTIONS = (
    ('as-preface', '前言与阅读指引', 'preface'),
    ('as-overview', '第一章　全息健康图景概览', 'overview'),
    ('as-symptoms', '第二章　核心主诉表型与多维客观指标关联解析', 'symptoms'),
    ('as-clinical', '第三章　分系统临床检验与亚临床功能双轨评估', 'clinical'),
    ('as-microbiome', '第四章　肠道微生态宏基因组与消化微环境深度评估', 'microbiome'),
    ('as-clusters', '第五章　系统失衡集群综合评估与生活诱因溯源', 'clusters'),
    ('as-conclusion', '第六章　综合健康评估结论与精准干预方向建议', 'conclusion'),
)
ORDERED_ASSESSMENT_VERSION = 'assessment.v2'

_INTRO_FIELDS = ('dataTiming', 'preface', 'readingGuide')
_BODY_FIELDS = ('bodyGroups', 'bodyMetrics')
_SYMPTOM_FIELDS = ('symptomsLead', 'symptoms', 'symptomSummary', 'symptomSummaryEvidenceRefs')
_CLUSTER_FIELDS = (
    'clusterSummary',
    'clusterSummaryEvidenceRefs',
    'clusters',
    'lifestyleSummary',
    'lifestyleSummaryEvidenceRefs',
    'lifestyleFactors',
    'lifestyleRecognition',
)


def _take(values: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: values.pop(key) for key in keys if key in values}


def order_assessment_for_delivery(view: dict[str, Any], appendices: dict[str, Any]) -> dict[str, Any]:
    """Group only display fields under introduction, numbered chapters and appendices."""
    overview = dict(view['overview'])
    clinical = dict(view['clinical'])
    microbiome = dict(view['microbiome'])
    conclusion = dict(view['conclusion'])
    introduction = {
        'title': view['title'],
        'prefaceTitle': ASSESSMENT_SECTIONS[0][1],
        'dataTiming': overview.pop('dataTiming', {}),
        'preface': overview.pop('preface', None),
        'readingGuide': overview.pop('readingGuide', []),
    }
    body_summary = _take(overview, ('bodySummary', 'bodySummaryEvidenceRefs'))
    body_metrics = _take(clinical, _BODY_FIELDS)
    symptoms = _take(clinical, _SYMPTOM_FIELDS)
    digestive = _take(clinical, ('digestiveContext',))
    sections = {
        'overview': {
            **body_summary,
            **body_metrics,
            **_take(
                overview,
                (
                    'summary',
                    'evidenceRefs',
                    'systems',
                    'priorityFindings',
                    'graphHub',
                    'graphClusterIds',
                    'graphSummary',
                    'graphSummaryEvidenceRefs',
                ),
            ),
            **overview,
        },
        'symptoms': symptoms,
        'clinical': {
            **_take(
                clinical,
                (
                    'summary',
                    'clinicalGroups',
                    'specialtyFindings',
                    'specialtySummary',
                    'specialtySummaryEvidenceRefs',
                    'trends',
                    'trendSummary',
                    'trendSummaryEvidenceRefs',
                    'normalBaselines',
                ),
            ),
            **clinical,
        },
        'microbiome': {
            **_take(
                microbiome,
                (
                    'overviewSummary',
                    'overviewSummaryEvidenceRefs',
                    'summary',
                    'evidenceRefs',
                    'sampleContext',
                    'sampleSummary',
                    'sampleSummaryEvidenceRefs',
                    'fermentationSummary',
                    'fermentationSummaryEvidenceRefs',
                ),
            ),
            **digestive,
            **_take(
                microbiome,
                (
                    'structureGroups',
                    'structure',
                    'structureSummary',
                    'structureSummaryEvidenceRefs',
                    'organismGroups',
                    'functionalGroups',
                    'riskSummary',
                    'riskSummaryEvidenceRefs',
                ),
            ),
            **microbiome,
        },
        'clusters': {key: view[key] for key in _CLUSTER_FIELDS},
        'conclusion': {
            **_take(conclusion, ('layers', 'summary', 'evidenceRefs', 'managementDirections', 'priorities')),
            **conclusion,
        },
    }
    return {
        'version': ORDERED_ASSESSMENT_VERSION,
        'introduction': introduction,
        'chapters': [
            {'number': number, 'code': code, 'title': title, 'content': sections[code]}
            for number, (_, title, code) in enumerate(ASSESSMENT_SECTIONS[1:], 1)
        ],
        'appendices': appendices,
    }


def assessment_view_for_rendering(data: Mapping[str, Any]) -> dict[str, Any] | None:
    """Read the ordered callback, historical flat view or historical nested view."""
    version = data.get('version')
    if version == 'assessment.v1':
        return {key: value for key, value in data.items() if key != 'appendices'}
    if version != ORDERED_ASSESSMENT_VERSION:
        view = data.get('assessmentPresentation')
        return view if isinstance(view, dict) and view.get('version') == 'assessment.v1' else None

    introduction = data['introduction']
    sections = {chapter['code']: chapter['content'] for chapter in data['chapters']}
    overview = dict(sections['overview'])
    microbiome = dict(sections['microbiome'])
    body = _take(overview, _BODY_FIELDS)
    digestive = _take(microbiome, ('digestiveContext',))
    return {
        'version': 'assessment.v1',
        'title': introduction['title'],
        'overview': {**overview, **{key: introduction[key] for key in _INTRO_FIELDS}},
        'clinical': {**body, **sections['symptoms'], **sections['clinical'], **digestive},
        'microbiome': microbiome,
        **sections['clusters'],
        'conclusion': sections['conclusion'],
    }


def _compact_normal_baselines(groups, *, summarize_rows: bool) -> None:
    for group in groups:
        baseline = group.get('normalBaseline')
        if not baseline:
            continue
        rows = baseline.get('rows') or []
        if rows:
            baseline.pop('names', None)
            if not summarize_rows:
                continue
            key, items = 'rows', rows
        else:
            key, items = 'names', baseline.get('names') or []
        # The renderer expands at most twelve baseline items. Larger lists
        # keep their original count and render only these four examples.
        if len(items) > 12:
            count = baseline.get('count')
            if type(count) is not int or count <= 0:
                baseline['count'] = len(items)
            baseline[key] = items[:4]


def compact_assessment_for_rendering(data: dict[str, Any]) -> dict[str, Any]:
    """Compact the delivery copy in place and return it for serialization."""
    clinical = data.get('clinical') or {}
    clinical.pop('medicationRecords', None)
    if clinical.get('bodyGroups'):
        clinical.pop('bodyMetrics', None)
    for group in clinical.get('clinicalGroups') or ():
        if group.get('code') != 'otherClinical' and any(row.get('rows') for row in group.get('templateRows') or ()):
            group.pop('rows', None)

    microbiome = data.get('microbiome') or {}
    if 'structureGroups' in microbiome and microbiome.get('structure'):
        # The flat rows only contribute section-heading codes on this branch.
        microbiome['structure'] = [{'code': row.get('code')} for row in microbiome['structure']]
    functions = microbiome.get('functionalGroups') or ()
    _compact_normal_baselines(microbiome.get('organismGroups') or (), summarize_rows=True)
    _compact_normal_baselines(functions, summarize_rows=False)

    for factor in data.get('lifestyleFactors') or ():
        factor.pop('entries', None)
        factor.pop('clusterIds', None)

    return data
