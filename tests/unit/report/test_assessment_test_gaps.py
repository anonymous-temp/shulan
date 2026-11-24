"""Test-gap rules: only tests absent from this person's records, with sources."""

from modules.report.assessment_test_gaps import GAP_RULES
from modules.report.assessment_test_gaps import SOURCES
from modules.report.assessment_test_gaps import TESTS
from modules.report.assessment_test_gaps import evaluate_test_gaps


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


def reported(fid, name, value):
    return {'factId': fid, 'kind': 'self_reported', 'name': name, 'value': value}


def codes(gaps):
    return {gap['code'] for gap in gaps}


def test_every_rule_has_reason_sources_and_known_tests():
    for rule in GAP_RULES:
        assert rule['reason'] and rule['sources'], rule['id']
        assert all(key in SOURCES for key in rule['sources'])
        assert all(code in TESTS for code in rule['tests'])


def test_dyslipidaemia_suggests_only_tests_not_already_done():
    facts = [
        lab('tg', '甘油三酯(TG)', '3.34', 'mmol/L', '0.00-1.70', '偏高'),
        lab('apob', '载脂蛋白B(apoB)', '0.99', 'g/L', '0.60-1.20'),
    ]

    gaps = evaluate_test_gaps(facts, {'sex': '男', 'age': 48})

    assert 'lpa' in codes(gaps)
    assert 'apoB' not in codes(gaps)
    gap = next(gap for gap in gaps if gap['code'] == 'lpa')
    assert gap['triggeredBy'] == ['tg'] and gap['sources'] and gap['reason']


def test_macrocytosis_skips_vitamin_b12_when_measured():
    facts = [
        lab('mcv', '平均红细胞体积', '104', 'fl', '82.0-100.0', '偏高'),
        lab('b12', '维生素B12', '420', 'pg/mL', '180-914'),
    ]

    assert codes(evaluate_test_gaps(facts, {'sex': '女', 'age': 45})) == {'folate', 'homocysteine'}


def test_low_rdw_is_not_treated_as_anisocytosis():
    facts = [lab('rdw', '红细胞分布宽度', '11.7', '%', '12.2-14.8', '偏低')]

    assert not evaluate_test_gaps(facts, {'sex': '男', 'age': 48})


def test_bone_density_screening_depends_on_sex_and_age():
    assert 'dxa' in codes(evaluate_test_gaps([], {'sex': '女', 'age': 66}))
    assert 'dxa' not in codes(evaluate_test_gaps([], {'sex': '男', 'age': 66}))
    assert 'dxa' in codes(evaluate_test_gaps([], {'sex': '男', 'age': 70}))


def test_gastric_screening_requires_age_forty_and_a_risk_factor():
    facts = [lab('hp', '碳13呼气试验DOB值(幽门螺杆菌)', '8.6', None, '-4.0-3.9', '阳性')]

    assert 'pepsinogen' not in codes(evaluate_test_gaps(facts, {'sex': '女', 'age': 35}))
    assert {'pepsinogen', 'gastrin17'} <= codes(evaluate_test_gaps(facts, {'sex': '女', 'age': 45}))
    salty = [reported('d', '您平时吃得咸吗？', '偏咸')]
    assert 'pepsinogen' in codes(evaluate_test_gaps(salty, {'sex': '男', 'age': 52}))
    declined = [reported('d', '您平时吃得咸吗？', '没有')]
    assert 'pepsinogen' not in codes(evaluate_test_gaps(declined, {'sex': '男', 'age': 52}))


def test_colorectal_screening_age_window_and_existing_stool_test():
    assert 'fit' in codes(evaluate_test_gaps([], {'sex': '男', 'age': 55}))
    assert 'fit' not in codes(evaluate_test_gaps([], {'sex': '男', 'age': 45}))
    done = [lab('ob', '粪便隐血试验', '阴性')]
    assert 'fit' not in codes(evaluate_test_gaps(done, {'sex': '男', 'age': 55}))


def test_refused_test_row_does_not_count_as_done():
    refused = [lab('x', '拒检项目', '粪便隐血本次体检未做')]

    assert 'fit' in codes(evaluate_test_gaps(refused, {'sex': '女', 'age': 60}))


def test_prediabetes_suggests_ogtt_and_the_missing_glycaemic_test():
    facts = [lab('g', '空腹葡萄糖(Glu)', '6.0', 'mmol/L', '3.89-6.11')]

    gaps = codes(evaluate_test_gaps(facts, {'sex': '男', 'age': 40}))

    assert {'ogtt', 'hba1c'} <= gaps
    assert 'fastingGlucose' not in gaps


def test_low_bone_mass_suggests_vitamin_d_and_merges_duplicate_test_rules():
    facts = [
        lab('dxa', '双能X线骨密度', '腰椎L1-L4 T值-2.1，股骨颈T值-1.8；骨量减少'),
        lab('sbp', '血压(收缩压)', '152', 'mmHg', '90-139'),
        lab('glu', '空腹葡萄糖', '7.6', 'mmol/L', '3.89-6.11'),
    ]

    gaps = evaluate_test_gaps(facts, {'sex': '女', 'age': 62})

    assert 'vitaminD' in codes(gaps)
    assert 'dxa' not in codes(gaps)
    uacr = [gap for gap in gaps if gap['code'] == 'uacr']
    assert len(uacr) == 1 and uacr[0]['alsoRuleIds']


def test_fatty_liver_suggests_viral_markers_and_fib4_drives_elastography():
    facts = [
        lab('us', '腹部彩超', '肝实质回声细密增强；脂肪肝'),
        lab('fib', '肝纤维化指数 FIB-4（计算值）', '1.62'),
    ]

    gaps = codes(evaluate_test_gaps(facts, {'sex': '男', 'age': 58}))

    assert {'hbsag', 'antiHcv', 'elastography'} <= gaps


def test_normal_person_without_age_screening_has_no_gaps():
    facts = [
        lab('tc', '总胆固醇(TC)', '4.5', 'mmol/L', '3.14-5.86'),
        lab('glu', '空腹葡萄糖', '4.9', 'mmol/L', '3.89-6.11'),
        lab('hb', '血红蛋白', '150', 'g/L', '130-175'),
    ]

    assert evaluate_test_gaps(facts, {'sex': '男', 'age': 29}) == []
