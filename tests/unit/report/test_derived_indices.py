from types import SimpleNamespace

from modules.report.evaluation_derived_indices import DERIVED_SOURCE
from modules.report.evaluation_derived_indices import append_derived_indices
from modules.report.evaluation_derived_indices import derived_index_facts
from modules.report.schemas import ClinicalReportDocument
from modules.report.schemas import ReportSourceFact


def _fact(index, name, value, unit=None):
    return ReportSourceFact(
        fact_id=f'f{index}', kind='clinical', name=name, value=value, unit=unit, date='2026-06-30', source='体检'
    )


def _facts():
    rows = [
        ('空腹葡萄糖(Glu)', '5.74', 'mmol/L'),
        ('空腹胰岛素(Ins)', '24.00', 'mIU/L'),
        ('总胆固醇(TC)', '5.88', 'mmol/L'),
        ('甘油三酯(TG)', '3.34', 'mmol/L'),
        ('高密度脂蛋白胆固醇(HDL-c)', '1.22', 'mmol/L'),
        ('低密度脂蛋白胆固醇(LDL-c)', '3.40', 'mmol/L'),
        ('天门冬氨酸氨基转移酶(AST)', '14', 'U/L'),
        ('丙氨酸氨基转移酶(ALT)', '17', 'U/L'),
        ('血小板计数', '175', '10E9/L'),
        ('身高', '171.7', 'cm'),
        ('体重', '86.3', 'kg'),
        ('腰围', '92', 'cm'),
        ('血压(收缩压)', '96', 'mmHg'),
        ('血压(舒张压)', '61', 'mmHg'),
        ('AST/ALT', '0.8', None),
        ('主检结论：腰围增大（男）92 cm', '中心型肥胖', None),
    ]
    return [_fact(index, *row) for index, row in enumerate(rows)]


def test_indices_follow_their_formulas():
    facts = {fact.name: fact for fact in derived_index_facts(_facts(), SimpleNamespace(age=48, sex='男'))}
    assert facts['胰岛素抵抗指数 HOMA-IR（计算值）'].value == '6.12'
    assert facts['非高密度脂蛋白胆固醇 non-HDL-C（计算值）'].value == '4.66'
    assert facts['残余胆固醇（计算值）'].value == '1.26'
    assert facts['甘油三酯/HDL-C 比值（计算值）'].value == '2.74'
    assert facts['肝纤维化指数 FIB-4（计算值）'].value == '0.93'
    bmi = facts['体重指数 BMI 及中国成人分级（计算值）']
    assert (bmi.value, bmi.status, bmi.abnormal) == ('29.3', '肥胖', True)
    assert facts['腰围及中心型肥胖判定（计算值）'].status == '中心型肥胖'
    mets = facts['代谢综合征组分计数（计算值）']
    assert mets.value == '2/5' and mets.abnormal is False
    assert all(fact.source.startswith(DERIVED_SOURCE) and fact.kind == 'clinical' for fact in facts.values())


def test_missing_or_conflicting_inputs_produce_no_index():
    facts = [fact for fact in _facts() if '胰岛素' not in fact.name]
    names = {fact.name for fact in derived_index_facts(facts, SimpleNamespace(age=48, sex='男'))}
    assert not any('HOMA' in name for name in names)
    conflicting = [*_facts(), _fact(99, '空腹血糖', '6.80', 'mmol/L')]
    names = {fact.name for fact in derived_index_facts(conflicting, SimpleNamespace(age=48, sex='男'))}
    assert not any('HOMA' in name for name in names) and '代谢综合征组分计数（计算值）' not in names
    no_age = {fact.name for fact in derived_index_facts(_facts(), SimpleNamespace(age=None, sex=None))}
    assert not any('FIB-4' in name or '腰围' in name for name in no_age)


def test_append_is_idempotent_and_never_recomputes_from_its_own_output():
    document = ClinicalReportDocument(facts=_facts())
    first = append_derived_indices(document, SimpleNamespace(age=48, sex='男'))
    assert first and append_derived_indices(document, SimpleNamespace(age=48, sex='男')) == []
    assert len({fact.fact_id for fact in document.facts}) == len(document.facts)
