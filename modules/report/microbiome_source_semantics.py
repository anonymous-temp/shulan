"""Private assay identities and cell duties for microbiome report authoring.

These describe source methods and reading responsibilities. They neither
regrade a source value nor supply a patient's medical interpretation.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from decimal import Decimal
from decimal import InvalidOperation

_COMPOSITION_STATE = re.compile(r'偏向|占优|优势|占主导|主导')
_SOURCE_LOCATOR = re.compile(r'^source:[a-fA-F0-9]{64}:(?:json:|page:\d+:template:)(.+)$')
_MEASUREMENT_MEANINGS = {
    'sequence_detection': 'target_sequence_signal',
    'functional_score': 'predicted_functional_potential',
    'source_graded_index': 'source_index',
    'source_health_association_assessment': 'source_health_association_assessment',
    'source_model_risk_assessment': 'source_model_risk_assessment',
}
_SEQUENCE_RESULT_MEANINGS = {
    'sequence_detected': 'sequence_detected',
    'detected_gene_sequences': 'sequence_detected',
    'sequence_not_detected': 'sequence_not_detected',
    'target_not_detected': 'sequence_not_detected',
}
_FUNCTION_DUTIES = {
    'carbohydrate': {
        'definition': '解释菌群对不同碳水底物的代谢功能预测，各底物的结果分别阅读。',
        'currentPattern': '对照本组偏离项与保存的正常项，说明偏离是局部还是一致，保留原分档和边界。',
        'crossTableMeaning': '结合真实糖代谢检查、饮食来源及目标中直接相关的事实，解释它们各自对判断的贡献。',
        'dietaryMeaning': '有真实膳食或临床依据时，可说明糖来源、频率和整体膳食结构的定性支持方向。',
        'relationCandidates': ['clinical_glucose_metabolism', 'dietary_carbohydrate_sources', 'metabolic_goals'],
    },
    'scfa': {
        'definition': '解释不同短链脂肪酸合成通路的预测潜能及其各自作用。',
        'currentPattern': '分别保留各酸的原报告方向、正常及偏离结果。共有端点已有逐项原分档时采用该档，'
                          '未给原档时保留相邻分档及原方向。',
        'crossTableMeaning': '与真实产短链脂肪酸菌面板的异常及完整正常底座合读，分清菌丰度、通路潜能和实际产物三层。',
        'dietaryMeaning': '依据科学原文和真实排便、饮食记录，可解释发酵底物支持及观察耐受的定性意义。',
        'relationCandidates': ['scfa_producer_assays', 'dietary_fermentable_substrates', 'reported_bowel_observations'],
    },
    'lipid': {
        'definition': '解释菌群脂质及类固醇相关通路的功能预测，区分各子通路的测量对象。',
        'currentPattern': '并列审阅偏高、偏低和正常项，说明方向分化或一致性，保留正常胆汁酸等反例。',
        'crossTableMeaning': '联系真实血脂、肝胆影像和代谢指标，各自承担菌群预测与临床判断的不同职责。',
        'dietaryMeaning': '有真实饮食或临床管理依据时，解释食物来源、脂肪质量和能量结构的定性意义。',
        'relationCandidates': ['clinical_lipid_metabolism', 'hepatobiliary_findings', 'dietary_fat_and_energy_sources'],
    },
    'neurotransmitter': {
        'definition': '解释菌群相关递质合成通路及其功能背景，测量对象是菌群预测潜能。',
        'currentPattern': '依照逐项原分档说明偏离广度与相对重点。整组同档时可以概括，存在不同档位时分别表达。',
        'crossTableMeaning': '有适用原文时，合读邻组氨基酸前体、辅因子通路与本人真实睡眠、精力或情绪记录。',
        'dietaryMeaning': '依据已有膳食和症状事实，说明饮食规律、睡眠及活动记录可提供的支持与观察价值。',
        'relationCandidates': [
            'amino_acid_precursor_pathways',
            'vitamin_cofactor_pathways',
            'reported_sleep_and_energy',
        ],
    },
    'aminoAcid': {
        'definition': '解释不同氨基酸合成通路的菌群功能预测及有依据的生理作用。',
        'currentPattern': '用完整分母比较偏弱与正常项，区分个别底物、功能家族和整组，突出真正最弱的部分。',
        'crossTableMeaning': '有适用原文时联系相关递质、黏膜或其他功能通路，并结合真实蛋白营养检查和食物来源。',
        'dietaryMeaning': '依据实际蛋白来源和已知份量信息，解释食物多样性或核实摄入的定性意义。',
        'relationCandidates': ['related_functional_pathways', 'clinical_protein_nutrition', 'reported_protein_sources'],
    },
    'vitamin': {
        'definition': '解释菌群维生素合成相关通路及有原文依据的辅因子作用。',
        'currentPattern': '分别列明极低、偏低、正常及共有端点的子项。汇总时才使用偏离或低于正常区间。',
        'crossTableMeaning': '有适用原文时，与相关氨基酸、递质或其他通路合读；临床营养指标按自己的实际检测意义使用。',
        'dietaryMeaning': '依据本人已有食物来源和适用营养证据，可说明全食物多样性与进一步核实的方向。',
        'relationCandidates': ['cofactor_related_pathways', 'clinical_nutrient_measurements', 'reported_food_sources'],
    },
    'nucleotidePlant': {
        'definition': '分别说明核苷酸合成代谢与天然色素合成代谢的功能预测，保持两个原始通路面板独立。',
        'currentPattern': '按原始通路分开比较正常与偏离的子项，保留各自分档。',
        'crossTableMeaning': '保留植物性食物种类、频率与份量作为独立饮食背景。联系通路或临床结果时使用直接适用的证据。',
        'dietaryMeaning': '据真实记录说明植物性食物多样性和份量核实的价值，区分已经报告的食物与尚未知的信息。',
        'relationCandidates': [
            'reported_plant_food_sources',
            'related_functional_pathways',
            'relevant_clinical_findings',
        ],
    },
    'harmfulMetabolism': {
        'definition': '分别解释内源代谢物合成与外源物质降解的预测潜能及方向含义。',
        'currentPattern': '对照合成与降解各自的偏离和正常项，说明偏离集中于哪一过程及其重点。',
        'crossTableMeaning': '仅结合本人已报告的暴露、烹调或环境记录和适用科学原文，解释其可观察意义。',
        'dietaryMeaning': '有适用证据时可给出定性的食物储存、烹调和暴露管理方向，区分一般知识与本人已发生的暴露。',
        'relationCandidates': ['reported_exposures', 'reported_cooking_and_storage', 'relevant_clinical_measurements'],
    },
}


def function_cell_duties() -> dict[str, dict]:
    """Return private, positive duties for the eight approved table units."""
    return {
        f'function.{code}': {
            **deepcopy(duty),
            'cellRole': 'interpretation',
            'evidenceRole': '原结果负责本次模式；适用科学原文负责机制及人群关联；真实临床或问卷负责个人联系。',
            'sectionSixRole': '具体执行安排统一由第六章组织，本格可以解释定性膳食支持及观察理由。',
        }
        for code, duty in _FUNCTION_DUTIES.items()
    }


def source_measurand_definition(row: dict, semantics: dict) -> dict | None:
    """Resolve an owned indicator's definition, independently of its value.

    Explicit source definitions take precedence. The registered fallback is
    restricted to identified structure assays; names or supporting records
    cannot assign a ratio identity to another observation.
    """
    explicit = semantics.get('measurandDefinition')
    if isinstance(explicit, dict) and isinstance(explicit.get('label'), str) and explicit['label'].strip():
        return deepcopy(explicit)
    if (
        semantics.get('objectScope') != 'measurement'
        or 'structure_metric' not in semantics.get('sourcePanelRoles', ())
    ):
        return None
    from .evaluation_catalog import OVERALL_STRUCTURE_DEFINITIONS

    identifier = semantics.get('measurandId') or row.get('code')
    label = dict(OVERALL_STRUCTURE_DEFINITIONS).get(identifier)
    if not label:
        return None
    definition = {
        'label': label,
        'measurandId': identifier,
        'origin': 'registered_source_protocol',
    }
    # Only an explicit parenthesized A/B in the registered definition owns
    # these roles. No acronym expansion or taxon-name matching is involved.
    ratio = re.search(r'[（(]([^()（）/]+)/([^()（）/]+)[）)]$', label)
    if ratio:
        definition['ratio'] = {
            'numerator': {'label': ratio[1].strip()},
            'denominator': {'label': ratio[2].strip()},
        }
    return definition


def source_function_panel_definition(semantics: dict) -> dict | None:
    """Keep an owned functional panel's registered meaning with its members."""
    explicit = semantics.get('panelDefinition')
    if isinstance(explicit, dict) and (explicit.get('label') or explicit.get('panels')):
        return deepcopy(explicit)
    if 'functional_prediction_panel' not in semantics.get('sourcePanelRoles', ()):
        return None
    from .evaluation_template_input import NUTRITION_PANELS

    panels = list(dict.fromkeys(semantics.get('sourcePanels') or ()))
    if panel := semantics.get('sourcePanel'):
        if panel not in panels:
            panels.append(panel)
    if not panels or any(panel not in NUTRITION_PANELS for panel in panels):
        return None
    definitions = []
    for panel in panels:
        definition = {
            'label': NUTRITION_PANELS[panel][0],
            'sourcePanel': panel,
            'origin': 'registered_source_protocol',
        }
        assays = [assay for assay in semantics.get('assays', ())
            if assay.get('sourcePath') == panel and assay.get('id') in semantics.get('assayIds', ())]
        # Preserve reported/registered methods only. The panel title does
        # not establish a sequencing method or a host concentration assay.
        for field in ('measurementBasis', 'method'):
            values = []
            for assay in assays:
                value = assay.get(field)
                if value is not None and value not in values:
                    values.append(value)
            if len(values) == 1:
                definition[field] = deepcopy(values[0])
        definitions.append(definition)
    if len(definitions) == 1:
        return definitions[0]
    return {
        'scope': 'multiple_owned_function_panels',
        'origin': 'registered_source_protocol',
        'panels': definitions,
    }


def source_assessment_semantics(source_path: str, *, category=None, family=None) -> dict | None:
    """Identify registered report assessments without turning them into assays."""
    from .evaluation_template_input import _ADULT_EXTENDED_DISEASE_FIELDS
    from .evaluation_template_input import _CHILD_DISEASE_FIELDS
    from .evaluation_template_input import DISEASE_FIELDS
    from .evaluation_template_input import HEALTH_ASSOCIATION_PANELS

    panel = source_path.removesuffix('_all_result')
    if category == 'healthAssociation' and source_path == panel + '_all_result':
        if panel not in HEALTH_ASSOCIATION_PANELS:
            return None
        label = HEALTH_ASSOCIATION_PANELS[panel]
        meaning = 'source_health_association_assessment'
        role = 'source_reported_microbiome_association'
    elif category == 'diseaseResults' and source_path.endswith('_score'):
        panel = source_path.removesuffix('_score')
        registry = (_CHILD_DISEASE_FIELDS if family == 'G1'
            else _ADULT_EXTENDED_DISEASE_FIELDS if family == 'GM' else DISEASE_FIELDS)
        if panel not in registry:
            return None
        label = registry[panel][0]
        meaning = 'source_model_risk_assessment'
        role = 'source_reported_microbiome_model_index'
    else:
        return None
    return {
        'kind': meaning,
        'measurementType': meaning,
        'objectScope': 'reported_assessment',
        'measurandId': source_path,
        'stateMeaning': role,
        'statusAuthority': 'source_reported',
        'assessmentDefinition': {
            'label': label,
            'sourcePanel': panel,
            'sourcePath': source_path,
            'origin': 'registered_source_protocol',
            'assessmentBasis': role,
            'clinicalScope': 'source_assessment_only',
        },
    }


def comparison_semantics(row: dict, existing: dict) -> dict:
    """Identify the comparison axes without declaring any source conflict."""
    state = str(existing.get('originalStatus') or row.get('status') or '').strip()
    reference = row.get('referenceRange')
    measurement = existing.get('measurementType') or 'source_measurement'
    scope = existing.get('objectScope') or 'measurement'
    reference_basis = (
        'source_grade_bands'
        if existing.get('referenceBands')
        else 'individual_comparator'
        if reference and re.search(r'个人对照|实足年龄|实际年龄', reference)
        else 'reported_reference_population'
        if reference
        else 'not_reported'
    )
    if measurement in {'source_health_association_assessment', 'source_model_risk_assessment'}:
        role, basis = 'source_assessment_classification', existing['stateMeaning']
    elif _COMPOSITION_STATE.search(state):
        role, basis = 'within_sample_composition', 'within_sample_relative_composition'
    elif measurement == 'sequence_detection':
        role, basis = 'sequence_detection', 'source_sequence_target_detection'
    elif scope == 'panel':
        role, basis = 'panel_summary', 'source_panel_member_assessments'
    elif reference_basis != 'not_reported':
        role, basis = 'reference_comparison', reference_basis
    else:
        role, basis = 'source_classification', 'source_reported_classification'
    result = {
        **existing,
        'measurandId': existing.get('measurandId') or str(row.get('code') or row.get('name') or ''),
        'statusRole': role,
        'comparisonBasis': basis,
        'referenceBasis': reference_basis,
        'statusReferenceComparable': basis == reference_basis and reference_basis != 'not_reported',
        'sourceConflictCriterion': 'same_measurand_same_assay_same_comparison_basis_and_opposing_claims',
    }
    if definition := source_measurand_definition(row, result):
        result['measurandDefinition'] = definition
    if panel_definition := source_function_panel_definition(result):
        result['panelDefinition'] = panel_definition
    return result


def source_measurement_type(semantics: dict) -> str | None:
    measurement = semantics.get('measurementType')
    return 'sequence_detection' if measurement == 'sequence_target' else measurement


def source_sequence_observation_result(semantics: dict, *, normalized_result=None) -> str | None:
    """One enum bridge for explicit sequence results; abundance never enters."""
    if source_measurement_type(semantics) != 'sequence_detection':
        return None
    if normalized_result in {'sequence_detected', 'sequence_not_detected'}:
        return normalized_result
    if result := _SEQUENCE_RESULT_MEANINGS.get(semantics.get('stateMeaning')):
        return result
    detection = semantics.get('detectionStatus')
    if detection in {'detected', 'not_detected'}:
        return 'sequence_' + detection
    state = semantics.get('displayStatus') or semantics.get('originalStatus') or semantics.get('sourceStatus')
    return {
        '检出': 'sequence_detected',
        '检出序列信号': 'sequence_detected',
        '未检出': 'sequence_not_detected',
        '未检出序列信号': 'sequence_not_detected',
    }.get(state)


def measurement_meaning(semantics: dict) -> str | None:
    measurement = source_measurement_type(semantics)
    if measurement == 'relative_abundance':
        return ('relative_abundance_panel_comparison' if semantics.get('objectScope') == 'panel'
            else 'sample_relative_abundance')
    if measurement == 'source_measurement' and 'structure_metric' in semantics.get('sourcePanelRoles', ()):
        return 'source_index'
    return _MEASUREMENT_MEANINGS.get(measurement)


def _reported_limit(semantics, field):
    """Only an explicitly sourced analytical limit owns a limit claim.

    A population reference, a grading band or a recorded zero is not an
    analytical detection/quantification limit. Missing limits stay explicit.
    """
    declared = semantics.get('analyticalLimits')
    supplied = declared.get(field) if isinstance(declared, dict) else None
    if (isinstance(supplied, dict) and supplied.get('provided') is True
            and supplied.get('value') is not None and supplied.get('sourceLocator')):
        return {**deepcopy(supplied), 'status': 'reported', 'provided': True}
    return {'status': 'not_reported', 'provided': False, 'value': None, 'unit': None, 'sourceLocator': None}


def _observation_reference_range(row, semantics):
    """Use only this observation's source range, then this fact's literal."""
    comparison = semantics.get('referenceComparison')
    candidates = (
        semantics.get('referenceText'),
        semantics.get('referenceRange'),
        comparison.get('referenceRange') if isinstance(comparison, dict) else None,
        row.get('referenceRange', row.get('reference_range')),
    )
    return deepcopy(next((value for value in candidates if value is not None and value != ''), None))


def typed_measurement_observation(row: dict, semantics: dict) -> dict:
    """One positive observation contract for source frames and claim grounding."""
    meaning = measurement_meaning(semantics)
    if meaning is None:
        return {}
    value = deepcopy(row.get('value'))
    present = value is not None and value != ''
    numeric = None
    if present and not isinstance(value, bool):
        try:
            literal = str(value).strip()
            if meaning == 'sample_relative_abundance':
                literal = literal.removesuffix('%').removesuffix('％')
            candidate = Decimal(literal)
            numeric = candidate if candidate.is_finite() else None
        except (InvalidOperation, ValueError):
            pass
    value_meaning = (
        'recorded_zero_relative_share' if meaning == 'sample_relative_abundance' and numeric == 0 else
        'recorded_relative_share' if meaning == 'sample_relative_abundance' else
        'reported_panel_reference_comparison' if meaning == 'relative_abundance_panel_comparison' else
        'reported_target_sequence_result' if meaning == 'target_sequence_signal' else
        'predicted_community_function_score' if meaning == 'predicted_functional_potential' else
        'reported_health_association_assessment_status' if meaning == 'source_health_association_assessment' else
        'reported_source_model_index' if meaning == 'source_model_risk_assessment' else
        'reported_source_index'
    )
    observed_value = {
        'state': 'reported' if present else 'not_reported',
        'value': value,
        'unit': deepcopy(row.get('unit')),
        'meaning': value_meaning,
    }
    if value_meaning == 'recorded_zero_relative_share':
        observed_value['readingConstraint'] = '原报告记录本样本的相对丰度为0%，只据此陈述样本相对丰度。'
    detection = 'not_reported'
    if meaning == 'target_sequence_signal':
        result = source_sequence_observation_result(semantics, normalized_result=row.get('result'))
        detection = {'sequence_detected': 'detected', 'sequence_not_detected': 'not_detected'}.get(
            result, 'not_reported',
        )
        if detection not in {'detected', 'not_detected'}:
            detection = 'not_reported'
    limits = {field: _reported_limit(semantics, field) for field in ('detectionLimit', 'quantificationLimit')}
    locators = sorted(set(semantics.get('observationLocators') or ()))
    # Labels are presentation aliases. Physical source locations, rather
    # than a Chinese/Latin spelling, identify the measured observation.
    identities = locators or ([json.dumps({
        'assayIds': sorted(semantics.get('assayIds') or ()),
        'measurandId': semantics.get('measurandId'),
    }, ensure_ascii=False, sort_keys=True)] if semantics.get('measurandId') else [])
    ids = ['micro-observation:' + hashlib.sha256((meaning + '|' + identity).encode()).hexdigest()[:24]
        for identity in identities]
    identifier = (ids[0] if len(ids) == 1 else 'micro-observation-set:' + hashlib.sha256(
        '|'.join(ids).encode(),
    ).hexdigest()[:24]) if ids else None
    comparison = {
        'status': deepcopy(row.get('status')),
        'statusRole': semantics.get('statusRole', 'not_reported'),
        'comparisonBasis': semantics.get('comparisonBasis', 'not_reported'),
        'referenceBasis': semantics.get('referenceBasis', 'not_reported'),
        'referenceRange': _observation_reference_range(row, semantics),
        'referenceBands': deepcopy(semantics.get('referenceBands') or []),
        'matchingBands': deepcopy(semantics.get('matchingBands') or []),
        'boundaryAmbiguous': semantics.get('boundaryAmbiguous', False),
        'resolvedGrade': semantics.get('resolvedGrade'),
    }
    if meaning == 'predicted_functional_potential':
        directions = list(dict.fromkeys(semantics.get('reportedDirections') or ()))
        status = str(semantics.get('originalStatus') or '').strip()
        if not directions and re.fullmatch('[↑↓]+', status):
            directions = [status]
        if directions:
            comparison['reportedDirections'] = directions
        if len(directions) == 1:
            comparison['originalDirection'] = directions[0]
        if semantics.get('gradeResolution') == 'source_reference_conflict':
            comparison['readingConstraint'] = '逐项原状态与按打印分档比较的结果不同，分别保留原记录。'
        elif comparison['boundaryAmbiguous']:
            comparison['readingConstraint'] = '并列原报告方向及全部共有端点分档。'
        elif comparison['resolvedGrade'] and len(comparison['matchingBands']) > 1:
            comparison['readingConstraint'] = '使用原报告明确的逐项分档，保留原方向和数值端点的分档重叠。'
        elif comparison['resolvedGrade']:
            comparison['readingConstraint'] = '使用该子项单独的已核分档，汇总覆盖范围时保留各分档差异。'
    return {
        'typedObservationProtocol': 'microbiome_source_observation_v2',
        'observationId': identifier,
        'observationIds': ids,
        'observationLocators': locators,
        'measurementMeaning': meaning,
        'observedValue': observed_value,
        'referenceComparison': comparison,
        'detection': {
            'status': detection,
            'provided': detection != 'not_reported',
            'objectScope': 'target_sequence' if meaning == 'target_sequence_signal' else 'not_reported',
        },
        'analyticalLimits': limits,
    }


def source_conflict_comparable(left: dict, right: dict) -> bool:
    """Eligibility only: opposing assertions still require source inspection."""
    basis = left.get('comparisonBasis')
    if not basis or basis != right.get('comparisonBasis'):
        return False
    if not left.get('measurandId') or left['measurandId'] != right.get('measurandId'):
        return False
    left_assays, right_assays = set(left.get('assayIds') or ()), set(right.get('assayIds') or ())
    return bool(left_assays and left_assays == right_assays)


def source_path_from_record(record_id: str) -> str | None:
    match = _SOURCE_LOCATOR.fullmatch(record_id or '')
    return match[1] if match else None


def _panel_organism_kind(panel):
    if panel.endswith(('_zj', '_fungi_species')):
        return 'fungi'
    if panel.endswith(('_bd', '_virus_species')):
        return 'virus'
    if panel.endswith(('_jsc', '_parasite_species')):
        return 'parasite'
    if panel.endswith(('_zbj', '_tjzbj')):
        return 'bacteria'
    # A functional guild or unspecified microbial chart is not itself a
    # taxonomic kingdom. Do not classify its members by their display names.
    return 'microbial_community'


def assay_identity(source_path: str, *, checksum=None, source_name=None, name=None, measurement_type=None) -> dict:
    """Classify an existing source field, never an organism's displayed name."""
    from .evaluation_template_input import HEALTH_ASSOCIATION_PANELS
    from .evaluation_template_input import NUTRITION_PANELS
    from .evaluation_template_input import STRUCTURE_FIELDS
    from .evaluation_template_input import TAXON_PANELS

    root = source_path.partition('[')[0].partition('.')[0]
    association = root.removesuffix('_all_result')
    reported_association = root != association and association in HEALTH_ASSOCIATION_PANELS
    # Resolve registered protocol aliases before the generic summary suffix.
    # Otherwise an alias can lose its identity before reaching its registry.
    aliases = {
        'nyjy_all': 'naiyao_gene_result',
        'nyjy_all_result': 'naiyao_gene_result',
        'health_summary_antibiotic_resistance_genes': 'naiyao_gene_result',
        'virulence_gene': 'virulence_gene_result',
        'virulence_gene_txt_result': 'virulence_gene_result',
    }
    root = aliases.get(root, root)
    if not reported_association:
        root = root.removesuffix('_all_result')
    summary_panels = [panel for panel, details in TAXON_PANELS.items() if details[2] == root]
    if len(summary_panels) == 1:
        root = summary_panels[0]
    if reported_association:
        title, assay_type = HEALTH_ASSOCIATION_PANELS[association], 'source_health_association_assessment'
        basis, organism_kind = 'source_reported_microbiome_association', None
    elif root in TAXON_PANELS:
        title = TAXON_PANELS[root][1]
        chart = root.startswith(('jun_top_', 'jun_top5_', 'jun_children_top'))
        assay_type = 'composition_chart' if chart else 'relative_abundance_panel'
        basis = 'within_sample_relative_abundance'
        organism_kind = _panel_organism_kind(root)
    elif summary_panels:
        # Shared compatibility summary codes do not identify adult/child
        # source cohorts. Preserve that identity gap instead of choosing one.
        title = TAXON_PANELS[summary_panels[0]][1]
        assay_type, basis = 'relative_abundance_panel', 'within_sample_relative_abundance'
        kinds = {_panel_organism_kind(panel) for panel in summary_panels}
        organism_kind = next(iter(kinds)) if len(kinds) == 1 else 'microbial_community'
    elif root in HEALTH_ASSOCIATION_PANELS:
        title, assay_type = HEALTH_ASSOCIATION_PANELS[root], 'association_abundance_panel'
        basis, organism_kind = 'within_sample_relative_abundance', 'microbial_community'
    elif root in NUTRITION_PANELS:
        title, assay_type = NUTRITION_PANELS[root][0], 'functional_prediction_panel'
        basis, organism_kind = 'source_functional_prediction', None
    elif root == 'virulence_gene_result':
        root, title, assay_type = 'virulence_gene_result', '毒力基因序列靶标面板', 'gene_sequence_panel'
        basis, organism_kind = 'source_sequence_target_detection', None
    elif root == 'naiyao_gene_result':
        root, title, assay_type = 'naiyao_gene_result', '抗生素耐药相关基因相对丰度', 'relative_abundance_panel'
        basis, organism_kind = 'within_sample_relative_abundance', None
    elif root in STRUCTURE_FIELDS:
        title, assay_type = STRUCTURE_FIELDS[root][1], 'structure_metric'
        basis, organism_kind = 'source_reported_metric', None
    elif measurement_type == 'source_model_risk_assessment':
        title, assay_type = name or root, 'source_model_risk_assessment'
        basis, organism_kind = 'source_reported_microbiome_model_index', None
    else:
        title, assay_type = name or root, 'source_measurement'
        basis, organism_kind = measurement_type or 'source_reported_measurement', None
    identity = '|'.join(str(value or '') for value in (checksum, source_name, root, assay_type))
    return {
        'id': 'micro-assay:' + hashlib.sha256(identity.encode()).hexdigest()[:20],
        'name': title,
        'type': assay_type,
        'organismKind': organism_kind,
        'measurementBasis': basis,
        'sourcePath': root,
        'sourceName': source_name,
        'sourceChecksum': checksum,
        'coverageRole': 'source_panel_members_only',
    }


def assay_groups(view: dict) -> list[dict]:
    """Full private assay catalogue, independent of selected display cells."""
    return deepcopy(view.get('_assayGroups', []))
