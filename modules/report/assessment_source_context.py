"""Model-facing source handles; the private provenance objects remain unchanged."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

from .microbiome_source_semantics import assay_identity
from .microbiome_source_semantics import measurement_meaning
from .microbiome_source_semantics import source_function_panel_definition
from .microbiome_source_semantics import source_measurand_definition
from .microbiome_source_semantics import source_measurement_type
from .microbiome_source_semantics import source_sequence_observation_result
from .microbiome_source_semantics import typed_measurement_observation


def assessment_reference_date():
    return datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()


def source_context_handles(value, handles):
    if isinstance(value, list):
        return [source_context_handles(item, handles) for item in value]
    if not isinstance(value, dict):
        return deepcopy(value)
    result = {}
    for key, item in value.items():
        if key == 'recordId' and isinstance(item, str):
            result[key] = handles.get(item, item)
        elif key in {'recordIds', 'sourceFactIds', 'relatedRecordIds', 'adviceRecordIds'} and isinstance(item, list):
            result[key] = [handles[fid] for fid in item if fid in handles]
        else:
            result[key] = source_context_handles(item, handles)
    return result


def assay_catalog_context(groups, handles):
    """Values live in records; an assay catalogue supplies identity and coverage."""
    return [
        source_context_handles({key: value for key, value in group.items() if key != 'members'}, handles)
        for group in groups
    ]


def model_source_semantics(entry, handles):
    """Reference the shared assay catalogue instead of nesting it per record.

    The full provenance entries remain private and unchanged. Assay identity,
    coverage and counterpart members are available once in patient.assayGroups;
    each record keeps its own comparison semantics and owning assay IDs.
    """
    compact = {
        key: value for key, value in entry.items() if key not in {'assays', 'counterpartAssays', 'sameKindAssayIds'}
    }
    return source_context_handles(compact, handles)


def _literal_microbiome_source(fact):
    aliases = {
        'name': ('name',),
        'value': ('value',),
        'unit': ('unit',),
        'status': ('status', 'sourceStatus', 'source_status'),
        'referenceRange': ('referenceRange', 'reference_range'),
    }
    return {field: deepcopy(next((fact[key] for key in keys if key in fact), None)) for field, keys in aliases.items()}


def _sequence_observation_result(semantics):
    return source_sequence_observation_result(semantics)


def _model_microbiome_observation(literal, semantics):
    measurement = source_measurement_type(semantics)
    if measurement_meaning(semantics) is None:
        return None
    scope = semantics.get('objectScope') or 'measurement'
    definition = source_measurand_definition(literal, semantics)
    if definition:
        semantics = {**semantics, 'measurandDefinition': definition}
    panel_definition = source_function_panel_definition(semantics)
    if panel_definition:
        semantics = {**semantics, 'panelDefinition': panel_definition}
    label = (definition or {}).get('label') or semantics.get('targetName') or semantics.get('reportedObject')
    if scope == 'reported_assessment':
        label = semantics.get('reportedObject') or label
    label = label or literal['name']
    row = {
        'label': label,
        'value': deepcopy(literal['value']),
        'unit': deepcopy(literal['unit']),
        'status': deepcopy(semantics.get('displayStatus', literal['status'])),
        'measurementType': measurement,
        'objectScope': scope,
        'assayIds': list(dict.fromkeys(semantics.get('assayIds') or ())),
        'organismKinds': list(dict.fromkeys(semantics.get('organismKinds') or ())),
        'interpretationBasis': {
            'temporalBasis': 'single_source_observation',
            'componentContributions': 'not_reported',
            'panelMemberRelationship': 'parallel_observations',
        },
    }
    for field in (
        'kind',
        'stateMeaning',
        'reportedObject',
        'targetName',
        'originalStatus',
        'statusAuthority',
        'candidateGenes',
        'detectedGenes',
        'measurandId',
        'measurandDefinition',
        'panelDefinition',
        'assessmentDefinition',
        'statusRole',
        'comparisonBasis',
        'referenceBasis',
        'statusReferenceComparable',
        'sourcePanel',
        'sourcePanels',
        'sourcePanelRoles',
        'referenceBands',
        'matchingBands',
        'boundaryAmbiguous',
        'resolvedGrade',
        'gradeResolution',
        'reportedDirections',
        'observationLocators',
    ):
        if field in semantics:
            row[field] = deepcopy(semantics[field])
    if panel_definition:
        row['memberLabel'] = deepcopy(literal['name'])
    panels = [*(semantics.get('sourcePanels') or ()), semantics.get('sourcePanel')]
    labels = {assay_identity(path)['name'] for path in panels if isinstance(path, str) and path}
    if labels.difference(panels):
        row['sourcePanelLabels'] = sorted(labels.difference(panels))
    if measurement == 'sequence_detection':
        candidates = semantics.get('candidateGenes') or []
        target = str(label or '来源检测对象')
        row['label'] = target + '相关靶标序列'
        if candidates:
            row['label'] += '（候选靶标：' + '、'.join(candidates) + '）'
        row['result'] = _sequence_observation_result(semantics)
        display = {
            'sequence_detected': '检出序列信号',
            'sequence_not_detected': '未检出序列信号',
        }.get(row['result'])
        row['value'] = row['status'] = display
        row['valueMeaning'] = 'source_target_sequence_detection'
    elif measurement == 'relative_abundance' and scope == 'panel':
        # An abundance panel's literal 未检出 is not a negative gene assay.
        # Keep that literal only in literalSource; the admitted display state
        # supplies the main model-facing result and owns only these assays.
        display = semantics.get('displayStatus')
        if semantics.get('stateMeaning') == 'absence_of_abundance_abnormality':
            display = '未检出异常'
        row['label'] = str(label or '来源面板') + '（相对丰度面板汇总）'
        row['value'] = row['status'] = display
        row['valueMeaning'] = 'source_panel_abundance_classification'
    else:
        row['valueMeaning'] = {
            'relative_abundance': 'reported_sample_relative_abundance',
            'functional_score': 'predicted_community_function_score',
            'source_graded_index': 'source_graded_community_index',
            'source_measurement': 'source_reported_community_index',
            'source_health_association_assessment': 'reported_health_association_assessment_status',
            'source_model_risk_assessment': 'reported_source_model_index',
        }[measurement]
    source_row = {**row, 'referenceRange': deepcopy(literal['referenceRange'])}
    row.update(typed_measurement_observation(source_row, semantics))
    return row


def _microbiome_claim(row):
    return {
        key: row.get(key)
        for key in (
            'label',
            'value',
            'unit',
            'status',
            'measurementType',
            'objectScope',
            'result',
            'measurandId',
            'measurandDefinition',
            'panelDefinition',
            'assessmentDefinition',
            'memberLabel',
            'statusRole',
            'comparisonBasis',
            'referenceBasis',
            'candidateGenes',
            'detectedGenes',
            'referenceBands',
            'matchingBands',
            'boundaryAmbiguous',
            'resolvedGrade',
            'measurementMeaning',
            'observedValue',
            'referenceComparison',
            'detection',
            'analyticalLimits',
        )
    }


def model_microbiome_observation(fact: dict, semantics: list[dict]) -> dict | None:
    """Project one source fact for both planners and writers, without mutation.

    Original name/value/status remain verbatim in literalSource. Assay IDs
    come only from this fact's authoritative semantic entries, never from a
    counterpart catalogue or another cell. A sequence-negative object is a
    target sequence; abundance zero remains the source's numeric abundance.
    """
    if not isinstance(fact, dict) or not isinstance(semantics, list):
        return None
    literal = _literal_microbiome_source(fact)
    observations = []
    for entry in semantics:
        if not isinstance(entry, dict):
            continue
        row = _model_microbiome_observation(literal, entry)
        if row is not None and row not in observations:
            observations.append(row)
    if not observations:
        return None
    # Multiple occurrences may establish the same observation under several
    # source assays. Merge only identical semantic claims; distinct source
    # objects stay explicit rather than letting the first one replace them.
    merged = []
    for row in observations:
        match = next((item for item in merged if _microbiome_claim(item) == _microbiome_claim(row)), None)
        if match is None:
            merged.append(deepcopy(row))
            continue
        for field in ('assayIds', 'organismKinds', 'sourcePanelRoles', 'sourcePanelLabels', 'observationLocators'):
            values = list(dict.fromkeys([*match.get(field, ()), *row.get(field, ())]))
            if values:
                match[field] = values
        for key, value in row.items():
            if key not in match:
                match[key] = deepcopy(value)
        panels = list(dict.fromkeys([*match.get('sourcePanels', ()), *row.get('sourcePanels', ())]))
        if panels:
            match['sourcePanels'] = panels
            match.pop('sourcePanel', None)
            if len(panels) == 1:
                match['sourcePanel'] = panels[0]
        # Aliases with the same measured claim retain the union of actual
        # physical observation IDs, never a label-derived identity.
        contract = {**match, 'referenceText': match['referenceComparison']['referenceRange']}
        match.update(typed_measurement_observation(match, contract))
    if len(merged) == 1:
        return {**merged[0], 'literalSource': literal}
    return {
        'label': literal['name'],
        'value': [
            {key: deepcopy(value) for key, value in row.items() if key != 'interpretationBasis'} for row in merged
        ],
        'unit': None,
        'status': None,
        'measurementType': 'multiple_source_observations',
        'measurementMeaning': 'multiple_source_observations',
        'typedObservationProtocol': 'microbiome_source_observation_v2',
        'observationIds': list(dict.fromkeys(oid for row in merged for oid in row['observationIds'])),
        'objectScope': 'source_record',
        'assayIds': list(dict.fromkeys(assay for row in merged for assay in row['assayIds'])),
        'organismKinds': list(dict.fromkeys(kind for row in merged for kind in row['organismKinds'])),
        'literalSource': literal,
        'observations': merged,
    }
