from modules.report.evaluation_presentation import ai_tells
from modules.report.evaluation_presentation import present_for_reader
from modules.report.evaluation_presentation import reader_text
from modules.report.evaluation_schema import DiseaseRiskItem
from modules.report.evaluation_schema import EvaluationData
from modules.report.evaluation_schema import MedicalFinding
from modules.report.evaluation_schema import MetricAssessment
from scripts.render_evaluation_report import _indicator_rows
from scripts.render_evaluation_report import render_evaluation_html


def test_other_parties_and_capture_markers_never_reach_the_reader():
    assert reader_text('主检结论：混合型高脂血症') == '混合型高脂血症'
    assert reader_text('人体成分体重控制建议(原报告)') == '人体成分体重控制建议'
    assert reader_text('检测方模型：高风险') == '菌群检测：高风险'
    assert reader_text('明显便秘：用户自述，未核验') == '明显便秘'
    assert reader_text('菌群检测将肠易激综合征和功能性便秘列为检出项，鼠李糖乳酪杆菌为0%。', prose=True) == (
        '菌群检测显示与肠易激综合征和功能性便秘相关的菌群变化，鼠李糖乳酪杆菌为0%。'
    )
    assert reader_text('HOMA-IR（计算值）6.12') == 'HOMA-IR 6.12'
    prose = reader_text(
        '值得注意的是，检测方菌群模型提示便秘相关——这是关联提示，不是诊断。'
        '您自述留取标本时有痔疮发作，这两项都是本人报告，未经检查确认。按原报告建议6个月后复查。',
        prose=True,
    )
    for banned in ('检测方', '原报告', '未经检查确认', '本人报告', '——', '值得注意', '不是诊断', '自述'):
        assert banned not in prose, banned
    # Facts in the same sentence as a disclaimer survive.
    assert '痔疮发作' in prose and '6个月后复查' in prose


def test_reader_wording_is_applied_to_displayed_strings_but_not_to_values_or_codes():
    data = EvaluationData()
    data.medical_assessment.findings = [
        MedicalFinding(
            code='templateProblem:x',
            name='主检结论：脂肪肝',
            value='腹部彩超：脂肪肝；原报告建议：复查',
            unit='mmol/L',
            reference_range='3.14-5.86',
            explanation='按主检意见复查——不是诊断。',
            certainty='current_problem',
        )
    ]
    data.disease_risk_assessment.risk_ranking = [
        DiseaseRiskItem(
            code='templateRisk:intestinal:1',
            name='肠易激综合征',
            source_status='检测关联提示',
            related_indicators=[MetricAssessment(name='嗜酸乳杆菌', source_status='偏低', value='0')],
        )
    ]
    changed = present_for_reader(data)
    finding = data.medical_assessment.findings[0]
    assert changed >= 3
    assert finding.name == '脂肪肝' and '原报告' not in finding.value and '主检' not in finding.explanation
    assert finding.code == 'templateProblem:x' and finding.certainty == 'current_problem'
    assert finding.reference_range == '3.14-5.86' and finding.unit == 'mmol/L'
    assert data.disease_risk_assessment.risk_ranking[0].source_status == '菌群关联'


def test_tells_are_named_for_rewrite_hints():
    assert set(ai_tells('这不是诊断，而是提示——值得注意的是，核心在于检测方模型。')) >= {
        '不是…而是',
        '破折号',
        '套话连接词',
        '空泛大词',
        '来源方',
    }
    assert ai_tells('甘油三酯 3.34 mmol/L，约为上限的 2 倍，建议 3 个月后复查。') == []


def test_result_text_becomes_chartable_rows_with_reference_ranges():
    rows, others = _indicator_rows(
        '总胆固醇(TC)：5.88 mmol/L（参考3.14-5.86；偏高）；腰围：92 cm（参考男性<90；偏高）；'
        '粪便隐血试验：弱阳性（参考阴性；阳性）；问卷：痔疮发作、明显便秘'
    )
    assert [(row['name'], row['value'], row['reference'], row['flag']) for row in rows] == [
        ('总胆固醇(TC)', 5.88, (3.14, 5.86), '偏高'),
        ('腰围', 92.0, (None, 90.0), '偏高'),
    ]
    assert others == ['粪便隐血试验：弱阳性（参考阴性；阳性）', '问卷：痔疮发作、明显便秘']


def test_the_reference_page_shows_no_internal_codes_and_works_on_phones():
    body = {
        'generatedAt': '2026-11-15T10:00:00Z',
        'subject': {'name': '样例', 'gender': '女', 'age': 60},
        'data': {
            'healthOverview': {
                'overallSummary': {'text': '总体情况平稳。'},
                'assessmentChart': [
                    {'code': 'metabolic', 'name': '代谢相关疾病', 'score': 58, 'assessmentLevel': '重点关注'}
                ],
                'keyFindings': [
                    {'rank': 1, 'name': '高血压', 'assessmentLevel': '重点管理', 'summary': '血压 152/94 mmHg。'}
                ],
            },
            'medicalAssessment': {
                'findings': [
                    {
                        'name': '高血压',
                        'value': '收缩压：152 mmHg（参考90-139；偏高）',
                        'certainty': 'current_problem',
                        'explanation': '血压高于正常。',
                        'sourceAdvice': '每天测血压。',
                    }
                ]
            },
        },
    }
    page = render_evaluation_html(body)
    for code in ('current_problem', 'partial_success', 'self_reported', '检测方', '主检'):
        assert code not in page
    assert 'max-width:720px' in page and 'class="rrow"' in page and '重点健康问题' in page
    # A formal report does not address the reader.
    assert '您' not in page and '一页看懂' not in page


def test_reader_quality_counts_what_must_not_reach_the_reader():
    from modules.report.evaluation_presentation import reader_quality

    data = EvaluationData()
    data.health_overview.overall_summary.text = '检测方模型提示风险——这不是诊断，而是提示。'
    data.microbiome_assessment.taxa = [MetricAssessment(name='烟曲霉')]
    quality = reader_quality(data)
    assert quality['otherPartyMentions'] >= 1 and quality['tellsByKind']['破折号'] == 1
    present_for_reader(data)
    clean = reader_quality(data)
    assert clean['otherPartyMentions'] == 0 and clean['englishCodes'] == 0 and '破折号' not in clean['tellsByKind']


def test_reader_wording_keeps_catalog_locked_sections_and_a_valid_callback():
    from modules.report.evaluation_callback import build_evaluation_callback  # noqa: F401  (contract owner)
    from modules.report.evaluation_schema import catalog

    data = EvaluationData()
    data.appendices.indicator_standards = EvaluationData.model_validate(
        {'appendices': {'indicatorStandards': catalog.APPENDIX_A_ROWS}}
    ).appendices.indicator_standards
    locked = data.appendices.model_dump(mode='json')
    present_for_reader(data)
    assert data.appendices.model_dump(mode='json') == locked
    EvaluationData.model_validate(data.model_dump(mode='json', by_alias=True))


def test_units_follow_the_lab_sheet_convention():
    text = reader_text('甘油三酯3.34毫摩尔每升，腰围92厘米，内脏脂肪面积107.3平方厘米。', prose=True)
    assert text == '甘油三酯3.34 mmol/L，腰围92 cm，内脏脂肪面积107.3 cm²。'


def test_the_report_is_impersonal_and_does_not_narrate_its_inputs():
    text = reader_text(
        '问卷显示，您每周有3~6天吃夜宵；菌群检测显示，您粪便中嗜酸乳杆菌为0%。建议您两周内复查，这对您有益。您48岁。',
        prose=True,
    )
    assert text == (
        '每周有3~6天吃夜宵；肠道菌群方面，粪便中嗜酸乳杆菌为0%。建议两周内复查，这对受检者有益。受检者48岁。'
    )
    # Item titles and paragraph breaks of an itemised cell are kept.
    cell = '结论：两处病灶均属低风险。\n\n甲状腺结节：您的结节0.4 cm，TI-RADS 3类。'
    assert reader_text(cell, prose=True) == '结论：两处病灶均属低风险。\n\n甲状腺结节：结节0.4 cm，TI-RADS 3类。'
    assert set(ai_tells('根据问卷，您平时没有运动。')) >= {'第二人称', '来源引出语'}
    # A second generic heading at the start of an item is dropped; a named result is not.
    assert reader_text('总体结论：整体判断：未见紧急病变。\n\n检查结果：甘油三酯：3.34 mmol/L。', prose=True) == (
        '总体结论：未见紧急病变。\n\n检查结果：甘油三酯：3.34 mmol/L。'
    )
    # "检测方法" and imaging wording are not source lead-ins.
    kept = '腹部超声提示脂肪肝，检测方法为宏基因组测序。'
    assert reader_text(kept, prose=True) == kept


def _integrated_body():
    return {
        'generatedAt': '2026-11-16T10:00:00Z',
        'subject': {'name': '样例', 'gender': '女', 'age': 60},
        'data': {
            'healthOverview': {
                'overallSummary': {'text': '总体结论：血压需要管理。\n\n重点问题：高血压，152/94 mmHg。'},
                'assessmentChart': [
                    {'code': 'metabolic', 'name': '代谢相关疾病', 'score': 58, 'assessmentLevel': '重点关注'},
                    {'code': 'tumor', 'name': '肿瘤相关疾病', 'score': 90, 'assessmentLevel': '关注度低（资料有限）'},
                    {'code': 'aging', 'name': '衰老相关疾病', 'score': 82, 'assessmentLevel': '关注度低'},
                ],
                'keyFindings': [
                    {'rank': 1, 'name': '高血压', 'assessmentLevel': '重点管理', 'summary': '血压 152/94 mmHg。'}
                ],
            },
            'medicalAssessment': {
                'findings': [
                    {
                        'name': '高血压',
                        'value': '收缩压：152 mmHg（参考90-139；偏高）；问卷：口味偏咸',
                        'explanation': '性质与程度：收缩压152 mmHg，属2级。\n\n临床意义：长期升高增加卒中风险。',
                        'sourceAdvice': '诊疗安排：心内科就诊。\n\n管理目标：低于130/80 mmHg。',
                    }
                ],
                'normalFindings': [{'name': '心电图', 'value': '正常范围'}],
            },
            'microbiomeAssessment': {
                'taxa': [{'name': '嗜酸乳杆菌', 'value': '0', 'unit': '%', 'referenceRange': '0.0001-0.0669'}]
            },
            'diseaseRiskAssessment': {
                'categories': [
                    {
                        'code': 'metabolic',
                        'name': '代谢相关疾病',
                        'attentionBasis': '血压达标管理',
                        'clinicalCorrelation': {'text': '结论：以血压管理为主。\n\n处理顺序：先监测后就诊。'},
                    }
                ],
                'overviewMatrix': [
                    {'code': 'metabolic', 'name': '代谢相关疾病', 'score': 58, 'attentionLevel': '重点关注'}
                ],
                'riskRanking': [
                    {'code': 'templateRisk:metabolic:1', 'name': '高血压', 'explanation': '风险分层：中危。'},
                    {
                        'code': 'templateRisk:metabolic:2',
                        'name': '肥胖症',
                        'sourceStatus': '菌群关联',
                        'explanation': '菌群特征：嗜酸乳杆菌未检出。',
                        'relatedIndicators': [
                            {'name': '嗜酸乳杆菌', 'value': '0', 'unit': '%', 'sourceStatus': '偏低'}
                        ],
                    },
                ],
            },
            'comprehensiveConclusion': {
                'priorityDirections': [
                    {
                        'rank': 1,
                        'name': '高血压',
                        'basis': '收缩压152 mmHg',
                        'followupAction': '2周内；家庭血压监测并就诊；心内科',
                    }
                ],
                'nextSteps': [{'name': '高血压', 'description': '事项：每日早晚测血压。\n\n完成时间：2周内就诊。'}],
            },
        },
    }


def test_each_problem_is_shown_once_under_its_domain_with_results_assessment_risk_and_management():
    page = render_evaluation_html(_integrated_body())
    body = page.split('</style>')[1]
    # Chapters follow health topics, not inputs.
    for old in ('体检与问卷', '三类检查的结论', '怎么理解', '怎么做'):
        assert old not in body
    domain = body.index('id="d-metabolic"')
    card = body.index('<article class="problem"')
    assert domain < card and body.count('<article class="problem"') == 1
    article = body[card : body.index('</article>')]
    for part in ('检查结果', '评估意见', '风险评估', '管理建议', 'class="rrow"', '口味偏咸'):
        assert part in article, part
    # Itemised text becomes labelled rows.
    assert '<dt>性质与程度</dt><dd>收缩压152 mmHg，属2级。</dd>' in article
    # Microbiome features sit inside the domain they relate to, with their reference range.
    assert body.index('肠道菌群相关特征') > card and '参考 0.0001-0.0669' in body
    # The priority table and the schedule read the structured first measure.
    assert '<td class="nw" data-l="时限">2周内</td>' in body and '家庭血压监测并就诊' in body and '心内科' in body
    assert 'class="mk"' in body and '随访与复查日程' in body
    # Tables and figures are numbered in order.
    assert '表 1\u3000重点健康问题及首要措施' in body and '图 1\u3000' in body


def test_time_limits_are_placed_on_the_schedule():
    from scripts.render_evaluation_report import _due
    from scripts.render_evaluation_report import _schedule_marks

    assert _due('2周内') == (0.5, None) and _due('3个月内') == (3.0, None) and _due('3~6个月') == (6.0, None)
    assert _due('每6个月') == (6.0, 6.0) and _due('每年') == (12.0, 12.0) and _due('尽快') == (0.5, None)
    assert _due('按专科约定') == (None, None)
    # Columns: 2周内, 1个月内, 3个月内, 6个月, 12个月.
    assert _schedule_marks('1个月内', '每6个月复查胆囊超声') == {1: 'first', 3: 're', 4: 're'}
    assert _schedule_marks('2周内', '仍阳性时行结肠镜') == {0: 'first'}
    assert _schedule_marks('1个月内', '6个月后复查血糖') == {1: 'first', 3: 're'}
    assert _schedule_marks('', '按复查结果确定') == {}
    assert _schedule_marks('2周内', '阳性者12个月内结肠镜') == {0: 'first'}
    assert _schedule_marks('1至3个月内', '每6至12个月复查肝功能和超声') == {2: 'first', 3: 're', 4: 're'}


def test_laboratory_abbreviations_are_spelled_out_in_prose():
    text = reader_text(
        'BMI 29.3 kg/m²，HOMA-IR 6.12，胰岛素抵抗指数（HOMA-IR）偏高，TG/HDL-C 2.9，CT未见异常。', prose=True
    )
    assert text == '体重指数29.3 kg/m²，胰岛素抵抗指数6.12，胰岛素抵抗指数偏高，TG/HDL-C 2.9，CT未见异常。'


def test_a_first_measure_that_misses_the_form_is_still_read_sensibly():
    from scripts.render_evaluation_report import _first_measure

    def measure(text):
        return _first_measure({'direction': {'followupAction': text}})

    assert measure('2周内；复查粪便常规和隐血；消化内科；仍阳性时尽早行结肠镜') == (
        '2周内',
        '复查粪便常规和隐血',
        '消化内科',
        '仍阳性时尽早行结肠镜',
    )
    assert measure('先到肝胆外科随诊评估，本次体检后近期完成；肝胆外科；此后每6个月复查胆囊超声') == (
        '近期',
        '先到肝胆外科随诊评估，本次体检后近期完成',
        '肝胆外科',
        '此后每6个月复查胆囊超声',
    )
