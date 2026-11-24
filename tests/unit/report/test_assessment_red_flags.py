"""Danger-signal rules: P0 comes from the rule table, not from report wording."""

from modules.report.assessment_red_flags import RULES
from modules.report.assessment_red_flags import SOURCES
from modules.report.assessment_red_flags import evaluate_red_flags
from modules.report.assessment_red_flags import interval_from_advice


def lab(fid, name, value, unit=None, reference=None, status=None):
    return {
        'factId': fid,
        'kind': 'clinical',
        'name': name,
        'value': value,
        'unit': unit,
        'referenceRange': reference,
        'status': status,
    }


def text(fid, name, value):
    return {'factId': fid, 'kind': 'clinical', 'name': name, 'value': value, 'imaging': True}


def reported(fid, name, value):
    return {'factId': fid, 'kind': 'self_reported', 'name': name, 'value': value}


def p0_ids(result):
    return {item['ruleId'] for item in result['priorityItems']}


def routine_ids(result):
    return {item['ruleId'] for item in result['routineFollowups']}


def test_every_rule_cites_known_sources_and_has_a_timeframe_or_interval():
    for rule in RULES:
        assert rule['sources'] and all(key in SOURCES for key in rule['sources']), rule['id']
        if rule['tier'] == 'P0':
            assert rule['timeframe']['text'] and rule['timeframe']['days'] is not None
            assert rule['department'] and rule['checks']
        else:
            assert rule['interval']['text'] and rule['department']


def test_positive_stool_occult_blood_is_p0_from_structured_result_alone():
    result = evaluate_red_flags([lab('a', '粪便隐血试验', '弱阳性', reference='阴性')], {'sex': '男', 'age': 48})

    item = result['priorityItems'][0]
    assert item['ruleId'] == 'RF-FOBT-POSITIVE'
    assert item['priority'] == 'P0'
    assert item['timeframe']['days'] == 30
    assert item['department'] == '消化内科'
    assert item['factIds'] == ['a']
    assert any(source.get('key') == 'cmab2019' for source in item['sources'])


def test_urine_occult_blood_does_not_count_as_stool_occult_blood():
    result = evaluate_red_flags([lab('a', '尿隐血', '1+', reference='阴性')], {'sex': '女', 'age': 40})

    assert 'RF-FOBT-POSITIVE' not in p0_ids(result)


def test_iron_deficiency_anaemia_in_postmenopausal_woman_requires_gi_workup():
    facts = [
        lab('hb', '血红蛋白', '108', 'g/L', '115-150', '偏低'),
        lab('mcv', '平均红细胞体积', '76.2', 'fl', '82.0-100.0', '偏低'),
        lab('fer', '铁蛋白(Fer)', '9.8', 'ng/mL', '13.0-150.0', '偏低'),
        reported('m', '月经情况', '已绝经10年'),
    ]

    result = evaluate_red_flags(facts, {'sex': '女', 'age': 62})

    item = next(item for item in result['priorityItems'] if item['ruleId'] == 'RF-IDA-GI')
    assert set(item['factIds']) == {'hb', 'mcv', 'fer'}
    assert item['department'] == '消化内科'


def test_iron_deficiency_rule_does_not_apply_to_premenopausal_woman():
    facts = [
        lab('hb', '血红蛋白', '108', 'g/L', '115-150'),
        lab('fer', '铁蛋白', '9.8', 'ng/mL', '13.0-150.0'),
        reported('m', '月经情况', '月经规律'),
    ]

    result = evaluate_red_flags(facts, {'sex': '女', 'age': 35})

    assert 'RF-IDA-GI' not in p0_ids(result)


def test_atrial_fibrillation_in_older_adult_is_p0_but_negated_mention_is_not():
    positive = evaluate_red_flags([text('e', '心电图', '心房颤动，心室率88次/分')], {'sex': '男', 'age': 78})
    negative = evaluate_red_flags([text('e', '心电图', '窦性心律，未见房颤')], {'sex': '男', 'age': 78})

    assert 'RF-ECG-AF' in p0_ids(positive)
    assert 'RF-ECG-AF' not in p0_ids(negative)


def test_second_degree_block_and_st_depression_are_p0():
    result = evaluate_red_flags([text('e', '心电图', '窦性心律，Ⅱ度Ⅰ型房室传导阻滞，ST段水平型压低')], {'age': 70})

    assert {'RF-ECG-AVB2', 'RF-ECG-STD'} <= p0_ids(result)


def test_critical_potassium_collapses_to_most_urgent_rule():
    result = evaluate_red_flags([lab('k', '钾(K)', '6.5', 'mmol/L', '3.5-5.3')], {'sex': '女', 'age': 81})

    assert p0_ids(result) == {'RF-K-CRITICAL'}
    assert result['priorityItems'][0]['timeframe']['days'] == 0


def test_moderate_hyponatraemia_and_hypokalaemia_are_p0():
    facts = [lab('na', '钠(Na)', '128', 'mmol/L', '137-147'), lab('k', '钾', '2.9', 'mmol/L', '3.5-5.3')]

    assert {'RF-NA-LOW', 'RF-K-MARKED'} <= p0_ids(evaluate_red_flags(facts, {'age': 66}))


def test_haemoglobin_in_g_per_dl_is_converted_before_threshold():
    result = evaluate_red_flags([lab('hb', 'HGB', '8.5', 'g/dL', '13.0-17.5')], {'sex': '男', 'age': 55})

    assert 'RF-HB-MODERATE' in p0_ids(result)


def test_aminotransferase_threshold_is_relative_to_upper_reference():
    marked = evaluate_red_flags([lab('alt', '丙氨酸氨基转移酶(ALT)', '260', 'U/L', '9-50')], {'age': 40})
    mild = evaluate_red_flags([lab('alt', '丙氨酸氨基转移酶(ALT)', '120', 'U/L', '9-50')], {'age': 40})

    assert 'RF-AMINOTRANSFERASE-5X' in p0_ids(marked)
    assert not mild['priorityItems']


def test_tumour_markers_use_two_times_upper_reference_or_specific_thresholds():
    marked = evaluate_red_flags([lab('cea', '癌胚抗原(CEA)', '12.0', 'ng/mL', '≤5.0')], {'age': 60})
    mild = evaluate_red_flags([lab('cea', '癌胚抗原(CEA)', '6.1', 'ng/mL', '≤5.0')], {'age': 60})
    psa = evaluate_red_flags(
        [lab('psa', '总前列腺特异性抗原(TPSA)', '12.4', 'ng/mL', '<4.000')], {'sex': '男', 'age': 66}
    )

    assert 'RF-TM-CEA-2XULN' in p0_ids(marked)
    assert not mild['priorityItems'] and 'RT-TM-CEA-MILD' in routine_ids(mild)
    assert 'RF-TM-PSA-10' in p0_ids(psa)


def test_ca125_threshold_depends_on_menopause():
    marker = lab('ca', '糖链抗原CA125', '120', 'U/mL', '<35.0')

    older = evaluate_red_flags([marker, reported('m', '月经情况', '已绝经5年')], {'sex': '女', 'age': 58})
    younger = evaluate_red_flags([marker, reported('m', '月经情况', '月经规律')], {'sex': '女', 'age': 32})

    assert 'RF-TM-CA125-POSTMENO' in p0_ids(older)
    assert not younger['priorityItems'] and 'RT-TM-CA125-MILD' in routine_ids(younger)


def test_imaging_grades_drive_p0_and_routine_tiers():
    facts = [
        text('b', '乳腺彩超', '左乳低回声结节，大小约1.2*0.8cm；BI-RADS 4A类'),
        text('t', '甲状腺彩超', '右叶低回声结节，大小约0.5*0.4cm；TI-RADS 3类'),
        text('l', '胸部低剂量CT', '右肺上叶实性结节；Lung-RADS 3类'),
    ]

    result = evaluate_red_flags(facts, {'sex': '女', 'age': 52})

    assert {'RF-BIRADS-4', 'RF-LUNGRADS-3'} <= p0_ids(result)
    assert 'RT-THYROID-NODULE' in routine_ids(result)
    assert 'RT-BREAST-NODULE' not in routine_ids(result)


def test_lung_nodule_size_and_calcification():
    large = evaluate_red_flags([text('l', '胸部CT', '左肺下叶实性结节，大小约9mm')], {'age': 60})
    calcified = evaluate_red_flags([text('l', '胸部CT', '左肺下叶见结节致密影；左肺下叶微小钙化灶')], {'age': 60})

    assert 'RF-LUNG-NODULE-8MM' in p0_ids(large)
    assert not calcified['priorityItems'] and not calcified['routineFollowups']


def test_negated_thyroid_nodule_creates_no_follow_up():
    result = evaluate_red_flags([text('t', '甲状腺彩超', '双侧叶未见明显结节；甲状腺弥漫性病变')], {'age': 50})

    assert not result['routineFollowups']


def test_routine_interval_comes_from_original_advice_before_rule_default():
    facts = [text('u', '腹部彩超', '胆囊壁见中等回声附着，长径约0.4cm，不随体位移动；胆囊息肉')]
    problem = {
        'code': 'P1',
        'name': '胆囊息肉',
        'factIds': ['u'],
        'originalAdvice': ['每6个月复查胆囊B超一次，肝胆外科随诊。'],
    }

    with_advice = evaluate_red_flags(facts, {'age': 48}, [problem])
    without_advice = evaluate_red_flags(facts, {'age': 48})

    item = with_advice['routineFollowups'][0]
    assert item['interval'] == {'text': '每6个月复查', 'days': 180, 'basis': 'original_advice'}
    assert item['problemCodes'] == ['P1']
    assert without_advice['routineFollowups'][0]['interval']['basis'] == 'rule_default'


def test_large_gallbladder_polyp_is_p0_and_suppresses_routine_tier():
    facts = [text('u', '腹部彩超', '胆囊内见息肉样隆起，大小约1.2*0.9cm；胆囊息肉')]

    result = evaluate_red_flags(facts, {'age': 45})

    assert 'RF-GB-POLYP-LARGE' in p0_ids(result)
    assert 'RT-GB-POLYP' not in routine_ids(result)


def test_helicobacter_positive_is_routine_not_p0():
    facts = [lab('hp', '碳13呼气试验DOB值(幽门螺杆菌)', '8.6', None, '-4.0-3.9', '阳性')]
    problem = {'code': 'HP', 'name': '幽门螺杆菌阳性', 'factIds': ['hp'], 'originalAdvice': ['建议复查，消化内科就诊']}

    result = evaluate_red_flags(facts, {'sex': '女', 'age': 62}, [problem])

    assert not result['priorityItems']
    assert 'RT-HP-POSITIVE' in routine_ids(result)


def test_urine_findings_need_renal_impairment_or_three_plus():
    with_renal = [lab('p', '尿蛋白质', '1+', reference='阴性'), lab('g', '估算肾小球滤过率(eGFR)', '52', 'mL/min')]
    without_renal = [lab('p', '尿蛋白质', '1+', reference='阴性'), lab('c', '肌酐(Cr)', '70', 'μmol/L', '57-97')]

    assert 'RF-URINE-WITH-RENAL' in p0_ids(evaluate_red_flags(with_renal, {'age': 68}))
    assert not evaluate_red_flags(without_renal, {'age': 68})['priorityItems']


def test_original_urgent_advice_is_a_supplementary_p0_source():
    facts = [text('x', '颈部血管彩超', '右侧颈内动脉起始处混合回声斑块')]
    urgent = {'code': 'C', 'name': '颈动脉斑块', 'factIds': ['x'], 'originalAdvice': ['建议尽快血管外科就诊。']}
    negated = {**urgent, 'originalAdvice': ['暂不需要尽快就诊，定期复查。']}

    result = evaluate_red_flags(facts, {'age': 70}, [urgent])

    item = result['priorityItems'][0]
    assert item['ruleId'] == 'RF-ADVICE-URGENT'
    assert item['department'] == '血管外科'
    assert item['timeframe']['basis'] == 'original_advice'
    assert not evaluate_red_flags(facts, {'age': 70}, [negated])['priorityItems']


def test_rule_p0_is_independent_of_original_wording():
    facts = [lab('a', '粪便隐血试验', '阳性')]
    advice_free = {'code': 'F', 'name': '粪便隐血阳性', 'factIds': ['a'], 'originalAdvice': []}

    result = evaluate_red_flags(facts, {'sex': '女', 'age': 70}, [advice_free])

    assert result['priorityItems'][0]['problemCodes'] == ['F']


def test_interval_parser_handles_chinese_numerals():
    assert interval_from_advice(['六个月后复查，建议OGTT。'])['days'] == 180
    assert interval_from_advice(['建议1年后复查。'])['days'] == 365
    assert interval_from_advice(['定期检查甲状腺B超。']) is None
