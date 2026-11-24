"""Clinical judgement layer of the six-chapter assessment (chapter 3, sections 1.1 and 1.2).

All rows and subjects are synthetic. Target behaviour uses a synthetic target
table so the rules are tested independently of the generated literature table.
"""

import random
import string
from decimal import Decimal
from types import SimpleNamespace

import pytest

from modules.report import assessment_clinical_rules as rules
from modules.report.assessment_clinical_view import _CLINICAL_GROUPS
from modules.report.assessment_clinical_view import _groups
from modules.report.assessment_clinical_view import _normal_baselines
from modules.report.assessment_clinical_view import _reader_group_status
from modules.report.assessment_clinical_view import _specialty_findings
from modules.report.assessment_clinical_view import _target_comparisons
from modules.report.assessment_clinical_view import attach_target_references
from modules.report.assessment_clinical_view import clinical_group_for
from modules.report.evaluation_derived_indices import derived_index_facts
from modules.report.schemas import ClinicalReportDocument
from modules.report.schemas import ReportSourceFact

MALE = rules.SubjectContext(sex='male', age=48, pregnancy='notPregnant')
FEMALE = rules.SubjectContext(sex='female', age=34, pregnancy='notPregnant')
SOURCE = {'title': '合成指南', 'year': 2024, 'doi': '10.0000/synthetic', 'url': 'https://example.org/guide'}


def _target(identifier, indicator, *, low=None, high=None, **conditions):
    return {
        'id': identifier,
        'indicator': indicator,
        'label': indicator,
        'unit': rules.target_table()['indicators'][indicator]['unit'],
        'low': low,
        'high': high,
        'lowInclusive': True if low is not None else None,
        'highInclusive': False if high is not None else None,
        'conditions': {
            'sex': None,
            'minAge': 18,
            'maxAge': None,
            'nonPregnant': True,
            'excludeConditions': [],
            'requiresConditions': [],
            **conditions,
        },
        'basis': '合成依据',
        'evidenceLevel': '指南',
        'sources': [SOURCE],
    }


@pytest.fixture
def synthetic_targets(monkeypatch):
    table = {
        'indicators': rules.target_table()['indicators'],
        'conditions': rules.target_table()['conditions'],
        'targets': [
            _target('hsCrp-1', 'hsCrp', high=1.0),
            _target('ldl-general', 'ldlC', high=2.6, excludeConditions=['ascvd', 'diabetes']),
            _target('ldl-diabetes', 'ldlC', high=1.8, requiresConditions=['diabetes']),
            _target('alt-male', 'alt', high=33, sex='male'),
            _target('alt-female', 'alt', high=25, sex='female'),
            _target('tg-1', 'tg', high=1.7),
            _target('homa-1', 'homaIr', high=2.5),
        ],
    }
    monkeypatch.setattr(rules, 'target_table', lambda: table)
    return table


def _row(name, value, unit=None, reference=None, status=None, **extra):
    return {
        'name': name,
        'value': value,
        'unit': unit,
        'referenceRange': reference,
        'templateStatus': status,
        'status': status,
        'specimen': None,
        'sourceSection': None,
        'sourceKind': 'clinical',
        '_sourceRole': 'direct_measurement',
        **extra,
    }


# ------------------------------------------------------------------ parsing


@pytest.mark.parametrize(
    ('text', 'kind', 'low', 'high'),
    [
        ('3.89-6.11', 'range', '3.89', '6.11'),
        ('3.89～6.11 mmol/L', 'range', '3.89', '6.11'),
        ('-4.0-3.9', 'range', '-4.0', '3.9'),
        ('0-1/HP', 'range', '0', '1'),
        ('<5.2', 'upper', None, '5.2'),
        ('≤380', 'upper', None, '380'),
        ('>60', 'lower', '60', None),
        ('≥1.04', 'lower', '1.04', None),
        ('阴性', 'qualitative', None, None),
        ('阴性(-)', 'qualitative', None, None),
        ('-', 'qualitative', None, None),
    ],
)
def test_reference_text_is_parsed(text, kind, low, high):
    reference = rules.parse_reference(text)
    assert reference.kind == kind
    assert reference.low == (Decimal(low) if low else None)
    assert reference.high == (Decimal(high) if high else None)


def test_sex_specific_reference_uses_the_subjects_part():
    assert rules.parse_reference('男:208-428；女:155-357', 'female').high == Decimal('357')
    assert rules.parse_reference('男:208-428；女:155-357', 'male').low == Decimal('208')
    assert rules.parse_reference('男:208-428；女:155-357', None) is None
    assert rules.parse_reference('男性<90', 'male').high == Decimal('90')


def test_values_are_single_numbers_only():
    assert rules.parse_value('<10') == (Decimal('10'), '<')
    assert rules.parse_value('5.74↑') == (Decimal('5.74'), None)
    assert rules.parse_value('120/80') == (None, None)
    assert rules.parse_value('阴性') == (None, None)


# ------------------------------------------------------------------ names and units


@pytest.mark.parametrize(
    ('name', 'unit', 'indicator'),
    [
        ('甘油三酯(TG)', 'mmol/L', 'tg'),
        ('血清甘油三酯测定', 'mg/dL', 'tg'),
        ('铁蛋白(Fer)', 'μg/L', 'ferritin'),
        ('γ-谷氨酰转移酶', 'U/L', 'ggt'),
        ('r-谷氨酰转移酶(GGT)', 'U/L', 'ggt'),
        ('天冬氨酸氨基转移酶', 'U/L', 'ast'),
        ('促甲状腺素', 'uIU/mL', 'tsh'),
        ('红细胞平均体积', 'fL', 'mcv'),
        ('葡萄糖', 'mmol/L', 'fpg'),
        ('空腹血糖(GLU)', 'mmol/L', 'fpg'),
        ('胰岛素', 'pmol/L', 'fastingInsulin'),
        ('血尿酸', 'μmol/L', 'uricAcid'),
        ('超敏C反应蛋白(hs-CRP)', 'mg/L', 'hsCrp'),
        ('空腹C肽(C-P)', 'ng/ml', 'cPeptide'),
        ('胰岛素抵抗指数 HOMA-IR（计算值）', None, 'homaIr'),
        ('体重指数 BMI 及中国成人分级（计算值）', 'kg/m2', 'bmi'),
        ('*低密度脂蛋白胆固醇(LDL-c)', 'mmol/L', 'ldlC'),
    ],
)
def test_hospital_writings_map_to_one_indicator(name, unit, indicator):
    assert rules.indicator_for(name, unit) == indicator


@pytest.mark.parametrize(
    'name',
    ['尿葡萄糖', '餐后2小时血糖', '胰岛素抗体', 'C反应蛋白(CRP)', '胰岛β细胞功能指数 HOMA-β（计算值）', '转铁蛋白'],
)
def test_similar_items_are_not_confused(name):
    assert rules.indicator_for(name) not in {'fpg', 'fastingInsulin', 'hsCrp', 'homaIr', 'ferritin'}


def test_urine_specimen_is_never_a_blood_indicator():
    assert rules.indicator_for('葡萄糖', 'mmol/L', specimen='尿') is None


@pytest.mark.parametrize(
    ('indicator', 'value', 'unit', 'expected'),
    [
        ('tg', '150', 'mg/dL', '1.69'),
        ('fpg', '100', 'mg/dL', '5.55'),
        ('ferritin', '120', 'μg/L', '120'),
        ('tsh', '2.1', 'uIU/mL', '2.1'),
        ('fastingInsulin', '60', 'pmol/L', '10'),
        ('hba1c', '39', 'mmol/mol', '5.72'),
        ('uricAcid', '7', 'mg/dL', '416.36'),
    ],
)
def test_units_convert_to_the_catalog_unit(indicator, value, unit, expected):
    converted = rules.to_canonical(indicator, Decimal(value), unit)
    assert converted.quantize(Decimal('0.01')) == Decimal(expected).quantize(Decimal('0.01'))


def test_unknown_unit_has_no_conversion():
    assert rules.to_canonical('tg', Decimal('1'), 'g/L') is None


# ------------------------------------------------------------------ four levels


def test_marked_deviation_is_focus(synthetic_targets):
    verdict = rules.judge(_row('甘油三酯(TG)', '3.34', 'mmol/L', '0.00-1.70', '偏高'), MALE)
    assert (verdict.level, verdict.direction) == ('focus', 'up')


def test_mild_deviation_is_borderline(synthetic_targets):
    verdict = rules.judge(_row('总胆固醇(TC)', '5.88', 'mmol/L', '3.14-5.86', '偏高'), MALE)
    assert (verdict.level, verdict.direction) == ('borderline', 'up')


def test_low_value_without_clinical_meaning_is_borderline(synthetic_targets):
    verdict = rules.judge(_row('天门冬氨酸氨基转移酶(AST)', '9', 'U/L', '15-40', '偏低'), MALE)
    assert (verdict.level, verdict.direction) == ('borderline', 'down')


def test_strict_analyte_is_focus_even_when_mild(synthetic_targets):
    assert rules.judge(_row('血钾', '5.4', 'mmol/L', '3.5-5.3'), MALE).level == 'focus'


def test_inside_clinical_range_but_outside_target_is_borderline(synthetic_targets):
    verdict = rules.judge(_row('超敏C反应蛋白(hs-CRP)', '1.61', 'mg/L', '0.00-6.00', '参考范围内'), MALE)
    assert verdict.level == 'borderline'
    assert verdict.direction == 'up'
    assert verdict.target['text'] == '<1 mg/L' and verdict.target['sources'][0]['doi'] == SOURCE['doi']


def test_inside_both_tracks_is_normal(synthetic_targets):
    verdict = rules.judge(_row('超敏C反应蛋白(hs-CRP)', '0.5', 'mg/L', '0.00-6.00'), MALE)
    assert (verdict.level, verdict.direction, verdict.target['state']) == ('normal', None, 'within')


def test_no_reference_and_no_rule_gives_no_level(synthetic_targets):
    assert rules.judge(_row('矫正视力(左眼)', '0.8'), MALE).level is None


def test_qualitative_positive_is_focus_and_negative_normal(synthetic_targets):
    assert rules.judge(_row('粪便隐血试验', '弱阳性', reference='阴性'), MALE).level == 'focus'
    assert rules.judge(_row('尿蛋白质', '阴性', reference='阴性'), MALE).level == 'normal'


def test_censored_value_inside_range_is_normal(synthetic_targets):
    assert rules.judge(_row('B型钠尿肽(BNP)', '<10', 'pg/ml', '0-100'), MALE).level == 'normal'


def test_unreferenced_derived_index_uses_the_target(synthetic_targets):
    row = _row('胰岛素抵抗指数 HOMA-IR（计算值）', '6.12', _sourceRole='derived_measurement', sourceKind='derived')
    assert rules.judge(row, MALE).level == 'focus'  # more than 50% beyond the target bound
    row['value'] = '2.9'
    assert rules.judge(row, MALE).level == 'borderline'


def test_structured_fields_are_added_without_changing_source_fields(synthetic_targets):
    row = _row('甘油三酯(TG)', '3.34', 'mmol/L', '0.00-1.70', '偏高')
    original = dict(row)
    rules.annotate_rows([row], MALE)
    assert {key: row[key] for key in original} == original
    assert row['valueNumber'] == 3.34
    assert (row['referenceLow'], row['referenceHigh'], row['referenceKind']) == (0, 1.7, 'range')
    assert (row['level'], row['direction']) == ('focus', 'up')
    assert row['target']['high'] == 1.7 and row['target']['evidenceRefs'] == []


def test_target_is_shown_in_the_rows_own_unit(synthetic_targets):
    verdict = rules.judge(_row('甘油三酯(TG)', '120', 'mg/dL'), MALE)
    assert verdict.target['high'] == 151  # 1.7 mmol/L shown in mg/dL
    assert verdict.target['text'].endswith('mg/dL')


# ------------------------------------------------------------------ applicability


def test_sex_specific_targets_follow_the_subject(synthetic_targets):
    row = _row('丙氨酸氨基转移酶(ALT)', '30', 'U/L', '9-50')
    assert rules.judge(row, MALE).level == 'normal'
    female = rules.judge(row, FEMALE)
    assert female.level == 'borderline' and female.target['text'].startswith('女性')


def test_minor_gets_no_adult_target(synthetic_targets):
    child = rules.SubjectContext(sex='male', age=15, pregnancy='notPregnant')
    verdict = rules.judge(_row('超敏C反应蛋白(hs-CRP)', '1.61', 'mg/L', '0.00-6.00'), child)
    assert verdict.target is None and verdict.level == 'normal'


def test_documented_pregnancy_removes_non_pregnant_targets(synthetic_targets):
    pregnant = rules.SubjectContext(sex='female', age=30, pregnancy='pregnant')
    assert rules.judge(_row('超敏C反应蛋白(hs-CRP)', '1.61', 'mg/L', '0.00-6.00'), pregnant).target is None


def test_missing_pregnancy_status_shows_target_without_escalating(synthetic_targets):
    unknown = rules.SubjectContext(sex='female', age=30, pregnancy='unknown')
    verdict = rules.judge(_row('超敏C反应蛋白(hs-CRP)', '1.61', 'mg/L', '0.00-6.00'), unknown)
    assert verdict.target['applicability'] == 'conditional'
    assert '非妊娠' in verdict.target['text']
    assert verdict.level == 'normal'


def test_older_woman_needs_no_pregnancy_record(synthetic_targets):
    older = rules.SubjectContext(sex='female', age=72, pregnancy='notPregnant')
    assert rules.judge(_row('超敏C反应蛋白(hs-CRP)', '1.61', 'mg/L', '0.00-6.00'), older).level == 'borderline'


def test_excluded_and_required_conditions_select_the_risk_tier(synthetic_targets):
    row = _row('低密度脂蛋白胆固醇(LDL-c)', '2.2', 'mmol/L', '0-3.37')
    assert rules.judge(row, MALE).target['id'] == 'ldl-general'
    diabetic = rules.SubjectContext(sex='male', age=48, pregnancy='notPregnant', conditions=frozenset({'diabetes'}))
    assert rules.judge(row, diabetic).target['id'] == 'ldl-diabetes'
    coronary = rules.SubjectContext(sex='male', age=48, pregnancy='notPregnant', conditions=frozenset({'ascvd'}))
    assert rules.judge(row, coronary).target is None


# ------------------------------------------------------------------ subject context


def _document(*answers):
    facts = [
        ReportSourceFact(fact_id=f'q{index}', kind='self_reported', name=name, value=value, source='用户自述：问卷')
        for index, (name, value) in enumerate(answers)
    ]
    return ClinicalReportDocument(facts=facts)


def test_recorded_conditions_respect_negation_and_family_history():
    document = _document(
        ('既往病史', '问题：既往病史；回答：高血压病史5年，否认糖尿病'),
        ('家族史', '问题：家族史；回答：父亲冠心病'),
    )
    context = rules.subject_context(SimpleNamespace(sex='男', age=55), document)
    assert 'hypertension' in context.conditions
    assert 'diabetes' not in context.conditions and 'ascvd' not in context.conditions


def test_thyroid_surgery_and_statin_use_are_recognised():
    document = _document(('手术史', '问题：手术史；回答：2019年甲状腺部分切除术后'), ('用药', '每日服用阿托伐他汀'))
    context = rules.subject_context(SimpleNamespace(sex='女', age=60), document)
    assert {'thyroidSurgery', 'lipidLoweringTherapy'} <= context.conditions


def test_pregnancy_from_hcg_and_history():
    subject = SimpleNamespace(sex='女', age=29)
    assert rules.subject_context(subject, None, [_row('β-HCG', '阳性')]).pregnancy == 'pregnant'
    assert rules.subject_context(subject, None, [_row('人绒毛膜促性腺激素', '1.2', 'mIU/mL', '0-5')]).pregnancy == (
        'notPregnant'
    )
    assert rules.subject_context(subject, _document(('现病史', '孕12周'))).pregnancy == 'pregnant'
    assert rules.subject_context(subject, None).pregnancy == 'unknown'
    assert rules.subject_context(SimpleNamespace(sex='男', age=29), None).pregnancy == 'notPregnant'
    assert rules.subject_context(SimpleNamespace(sex='女', age=66), None).pregnancy == 'notPregnant'


# ------------------------------------------------------------------ body and national criteria


@pytest.mark.parametrize(
    ('name', 'value', 'unit', 'context', 'level', 'direction'),
    [
        ('体重指数(BMI)', '24.5', 'kg/m2', MALE, 'borderline', 'up'),
        ('体重指数(BMI)', '29.3', 'kg/m2', MALE, 'focus', 'up'),
        ('体重指数(BMI)', '17.9', 'kg/m2', FEMALE, 'borderline', 'down'),
        ('腰围', '88', 'cm', MALE, 'borderline', 'up'),
        ('腰围', '86', 'cm', FEMALE, 'focus', 'up'),
        ('血压', '135/85', 'mmHg', MALE, 'borderline', 'up'),
        ('血压', '145/95', 'mmHg', MALE, 'focus', 'up'),
        ('血压', '118/76', 'mmHg', MALE, 'normal', None),
        ('血压(收缩压)', '96', 'mmHg', MALE, 'normal', None),
        ('心率', '56', '次/分', MALE, 'borderline', 'down'),
        ('肾小球滤过率(GFR-Cr)', '75', 'mL/min', MALE, 'borderline', 'down'),
    ],
)
def test_national_criteria_grade_body_rows(name, value, unit, context, level, direction):
    verdict = rules.judge(_row(name, value, unit), context)
    assert (verdict.level, verdict.direction) == (level, direction)


def test_adult_body_criteria_are_not_applied_to_minors_or_pregnancy():
    minor = rules.SubjectContext(sex='female', age=15, pregnancy='notPregnant')
    pregnant = rules.SubjectContext(sex='female', age=30, pregnancy='pregnant')
    assert rules.judge(_row('体重指数(BMI)', '26', 'kg/m2'), minor).level is None
    assert rules.judge(_row('腰围', '92', 'cm'), pregnant).level is None


def test_printed_body_reference_still_applies_without_a_national_rule():
    verdict = rules.judge(_row('基础代谢率', '1683', 'kcal', '1799-2117', '偏低'), MALE)
    assert (verdict.level, verdict.direction) == ('borderline', 'down')


@pytest.mark.parametrize(
    ('text', 'level'),
    [
        ('左侧甲状腺结节 TI-RADS 3类', 'borderline'),
        ('右侧甲状腺结节 C-TIRADS 4a类', 'focus'),
        ('左乳低回声结节 BI-RADS 4类', 'focus'),
        ('胆囊息肉，长径约0.4cm', 'borderline'),
        ('胆囊息肉，大小约12mm', 'focus'),
        ('脂肪肝', 'focus'),
        ('牙石1度', 'borderline'),
        ('颈椎退行性变', 'borderline'),
    ],
)
def test_source_findings_are_graded(text, level):
    assert rules.finding_level(text) == level


# ------------------------------------------------------------------ grouping (no item is dropped)


@pytest.mark.parametrize(
    ('name', 'group'),
    [
        ('乙肝表面抗原(HBsAg)', 'hematologyImmune'),
        ('抗核抗体', 'hematologyImmune'),
        ('人乳头瘤病毒(HPV)分型', 'hematologyImmune'),
        ('凝血酶原时间(PT)', 'hematologyImmune'),
        ('乳腺彩超', 'endocrineMusculoskeletal'),
        ('胸部低剂量CT平扫', 'endocrineMusculoskeletal'),
        ('宫颈液基细胞学(TCT)', 'endocrineMusculoskeletal'),
        ('耳鼻喉科检查', 'endocrineMusculoskeletal'),
        ('脑血管功能检测', 'specialty'),
        ('动脉硬化检测(ABI/baPWV)', 'specialty'),
        ('糖尿病早期风险筛查(EZSCAN)', 'specialty'),
        ('颈动脉彩超', 'cardiometabolic'),
        ('外院新增指标XYZ', 'otherClinical'),
    ],
)
def test_items_are_assigned_by_the_chapter_rules(name, group):
    assert clinical_group_for(name) == group


def test_random_item_names_always_have_a_home():
    generator = random.Random(20261124)
    alphabet = string.ascii_letters + string.digits + '检验指标测定值比率总游离抗体型'
    for _ in range(300):
        name = ''.join(generator.choice(alphabet) for _ in range(generator.randint(1, 14)))
        group = clinical_group_for(name, sex=generator.choice(['男', '女', None]))
        assert group is not None or rules.indicator_for(name) is None and _tumor(name)


def _tumor(name):
    from modules.report.assessment_clinical_view import _tumor_marker

    return _tumor_marker(name)


def test_grouping_keeps_every_row_outside_device_studies_and_tumor_markers():
    names = ['甘油三酯(TG)', '乙肝表面抗原', '乳腺彩超', '外院新增指标XYZ', '皮肤科检查', '癌胚抗原(CEA)', 'TCD']
    rows = [{**_row(name, '1'), 'code': name} for name in names]
    groups = _groups(rows, _CLINICAL_GROUPS, sex='女')
    placed = {row['name']: group['code'] for group in groups for row in group['rows']}
    assert placed['外院新增指标XYZ'] == 'otherClinical'
    assert placed['乙肝表面抗原'] == 'hematologyImmune'
    assert set(names) - set(placed) == {'癌胚抗原(CEA)', 'TCD'}  # baseline/abnormal finding and 3.5 device list
    other = next(group for group in groups if group['code'] == 'otherClinical')
    assert other['templateRows'][0]['name'] in {'其他检查', '外院新增指标XYZ'}


def test_hcg_is_a_hormone_test_for_women_and_a_tumor_marker_for_men():
    assert clinical_group_for('β-HCG', sex='女') == 'endocrineMusculoskeletal'
    assert clinical_group_for('β-HCG', sex='男') is None
    row = {**_row('β-HCG', '0.5', 'mIU/mL', '0-5', '参考范围内'), 'specimen': None}
    female = _normal_baselines([row], '女')
    assert not any(group['code'] == 'screening' for group in female)
    male = _normal_baselines([row], '男')
    assert next(group for group in male if group['code'] == 'screening')['rows'] == [row]
    abnormal = {**row, 'value': '120', 'templateStatus': '偏高'}
    assert _specialty_findings([abnormal], {}, '女') == []
    assert _specialty_findings([abnormal], {}, '男') == [abnormal]


def test_sex_specific_organ_imaging_counts_as_a_confirmed_baseline():
    breast = {**_row('乳腺彩超', '双乳未见明显异常', status='所见正常'), 'specimen': None}
    prostate = {**_row('前列腺彩超', '前列腺未见明显异常', status='所见正常'), 'specimen': None}
    female = {group['code']: group for group in _normal_baselines([breast], '女')}
    assert female['organFunction']['rows'] == [breast]
    male = {group['code']: group for group in _normal_baselines([prostate], '男')}
    assert male['organFunction']['rows'] == [prostate]


def test_device_studies_are_always_kept_for_section_3_5():
    row = {**_row('动脉硬化检测(ABI/baPWV) · baPWV', '1222', 'cm/s'), 'specimen': None, 'templateStatus': None}
    assert _specialty_findings([row], {}, '男') == [row]


# ------------------------------------------------------------------ reader wording


def test_panel_status_uses_unified_levels_and_no_internal_words(synthetic_targets):
    rows = [
        {**_row('超敏C反应蛋白(hs-CRP)', '1.61', 'mg/L', '0.00-6.00', '参考范围内'), '_ownRecordId': 'a'},
        {**_row('类风湿因子(RF)', '8.3', 'IU/ml', '≤30.0', '参考范围内'), '_ownRecordId': 'b'},
    ]
    targets = _target_comparisons(rows, MALE)
    status = _reader_group_status(rows, targets, MALE)
    assert '亚临床边界' in status and '稳健达标' in status
    assert 'hsCrp高于目标区间' in status
    for word in ('原件', '模板', '原报告', '未作统一分级'):
        assert word not in status
    assert _reader_group_status([_row('矫正视力', '0.8')], (), MALE) == '未给出参考区间'


def test_target_sources_are_numbered_after_the_bibliography(synthetic_targets):
    from modules.report.evaluation_schema import ReferenceLiterature

    library = [
        ReferenceLiterature(number=1, title='已有文献', doi='10.1/a'),
        ReferenceLiterature(number=2, title='合成指南', doi='10.0000/synthetic'),
    ]
    data = SimpleNamespace(appendices=SimpleNamespace(reference_literature=library))
    row = _row('超敏C反应蛋白(hs-CRP)', '1.61', 'mg/L', '0.00-6.00')
    rules.annotate_rows([row], MALE)
    other = dict(SOURCE, title='另一篇', doi='10.0000/other')
    second = _row('甘油三酯(TG)', '1.2', 'mmol/L', '0-1.7')
    rules.annotate_rows([second], MALE)
    second['target']['sources'] = [other]
    view = {'bodyMetrics': [], 'clinicalGroups': [{'rows': [row], 'templateRows': [{'rows': [second]}]}]}
    added = attach_target_references(view, data)
    assert row['target']['evidenceRefs'] == [2]  # an existing entry is reused
    assert second['target']['evidenceRefs'] == [3] and added == [3]
    assert library[-1].title == '另一篇' and library[-1].year == '2024'


# ------------------------------------------------------------------ derived indices


def _fact(index, name, value, unit=None):
    return ReportSourceFact(
        fact_id=f'f{index}', kind='clinical', name=name, value=value, unit=unit, date='2026-06-30', source='体检'
    )


def test_homa_ir_uses_routine_glucose_and_insulin_written_without_fasting():
    facts = [_fact(1, '葡萄糖(GLU)', '5.4', 'mmol/L'), _fact(2, '胰岛素(INS)', '10.0', 'uIU/mL')]
    homa = {fact.name: fact for fact in derived_index_facts(facts, SimpleNamespace(age=40, sex='女'))}
    assert homa['胰岛素抵抗指数 HOMA-IR（计算值）'].value == '2.40'
    assert '体检常规空腹采血' in homa['胰岛素抵抗指数 HOMA-IR（计算值）'].source


def test_homa_ir_converts_insulin_from_pmol_per_litre():
    facts = [_fact(1, '空腹血糖', '5.4', 'mmol/L'), _fact(2, '空腹胰岛素', '60', 'pmol/L')]
    names = {fact.name: fact.value for fact in derived_index_facts(facts, SimpleNamespace(age=40, sex='女'))}
    assert names['胰岛素抵抗指数 HOMA-IR（计算值）'] == '2.40'


def test_homa_ir_is_not_computed_from_a_postprandial_or_non_fasting_draw():
    facts = [_fact(1, '餐后2小时血糖', '7.4', 'mmol/L'), _fact(2, '胰岛素', '30', 'uIU/mL')]
    assert not any('HOMA' in fact.name for fact in derived_index_facts(facts, SimpleNamespace(age=40, sex='男')))
    routine = [_fact(1, '葡萄糖', '5.4', 'mmol/L'), _fact(2, '胰岛素', '10', 'uIU/mL')]
    stated = derived_index_facts(routine, SimpleNamespace(age=40, sex='男'), reported_fasting=False)
    assert not any('HOMA' in fact.name for fact in stated)


def test_pregnancy_suppresses_adult_bmi_and_waist_classification():
    facts = [_fact(1, '身高', '160', 'cm'), _fact(2, '体重', '70', 'kg'), _fact(3, '腰围', '92', 'cm')]
    pregnant = {
        fact.name: fact for fact in derived_index_facts(facts, SimpleNamespace(age=30, sex='女'), pregnant=True)
    }
    assert '体重指数 BMI（计算值）' in pregnant and pregnant['体重指数 BMI（计算值）'].status is None
    assert '腰围及中心型肥胖判定' not in pregnant
    adult = {fact.name: fact for fact in derived_index_facts(facts, SimpleNamespace(age=30, sex='女'))}
    assert adult['体重指数 BMI 及中国成人分级（计算值）'].status == '超重'
    assert adult['腰围及中心型肥胖判定'].status == '中心型肥胖'


def test_group_level_is_the_worst_member_with_named_anchors():
    rows = [
        {'name': f'指标{index}', 'value': str(index), 'unit': None, 'level': level, 'direction': 'up'}
        for index, level in enumerate(['normal', 'borderline', 'focus', *['borderline'] * 6, None])
    ]
    summary = rules.summarize_levels(rows)
    assert summary['level'] == 'focus'
    assert summary['anchors'][0]['name'] == '指标2' and len(summary['anchors']) == 6
    assert summary['moreCount'] == 2  # one focus and seven borderline members, six shown
    assert rules.summarize_levels([{'name': 'x', 'value': '1', 'level': None}])['level'] is None
