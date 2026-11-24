"""Offline checks of scripts/check_assessment_report.py on small synthetic callbacks."""

import json
import sys
import types

import pytest

from scripts import check_assessment_report as check


def _row(name, value, unit=None, **extra):
    return {'code': f'row-{name}', 'name': name, 'value': value, 'unit': unit, 'sources': [], **extra}


def _body(**overrides):
    """A minimal assessment.v2 callback; every chapter has content unless overridden."""
    contents = {
        'overview': {
            'bodySummary': '腰围92 cm属中心型肥胖。',
            'bodyGroups': [{'code': 'weightWaist', 'name': '身高、体重与围度', 'rows': [_row('腰围', '92', 'cm')]}],
            'bodyMetrics': [_row('腰围', '92', 'cm')],
            'systems': [{'code': 'cardiometabolic', 'name': '糖脂代谢', 'summary': 'TG 3.34 mmol/L 高于参考区间。'}],
            'priorityFindings': [
                {'code': 'p0', 'name': '粪便隐血阳性复查', 'priority': 'P0', 'text': '粪便隐血阳性。'}
            ],
            'graphSummary': '代谢失衡为主。',
            'graphClusterIds': ['C1'],
        },
        'symptoms': {
            'symptomsLead': '本次未记录主诉症状。',
            'symptoms': [],
            'symptomSummary': '主要异常与日常感受的关联：甘油三酯升高通常无明显症状。',
        },
        'clinical': {
            'clinicalGroups': [
                {
                    'code': 'cardiometabolic',
                    'name': '糖脂代谢',
                    'summary': '甘油三酯3.34 mmol/L升高。',
                    'rows': [_row('甘油三酯(TG)', '3.34', 'mmol/L')],
                }
            ],
            'trends': [],
            'trendSummary': '本次资料未包含可比较的历次结果。',
            'normalBaselines': [{'code': 'organ', 'name': '核心器官功能', 'rows': [_row('肌酐', '82', 'μmol/L')]}],
        },
        'microbiome': {
            'sampleSummary': '采样前用药情况未记录。',
            'digestiveContext': [{'code': 'oral', 'name': '口腔', 'summary': '口腔检查未见异常。'}],
            'structureGroups': [
                {'code': 'overall', 'name': '总评分', 'summary': '总评分53.34分。', 'rows': [_row('总评分', '53.34')]}
            ],
            'structure': [_row('总评分', '53.34')],
            'organismGroups': [{'code': 'beneficial', 'name': '有益菌', 'summary': '有益菌偏低。'}],
            'functionalGroups': [{'code': 'carbohydrate', 'name': '碳水', 'summary': '半乳糖评分偏高。'}],
            'riskSummary': '健康管理以临床检查结果为准。',
        },
        'clusters': {
            'clusters': [{'id': 'C1', 'name': '粪便隐血阳性复查', 'priority': 'P0', 'interpretation': '需排查出血。'}],
            'lifestyleFactors': [{'code': 'diet', 'name': '饮食', 'interpretation': '甜食每周3~6天。'}],
        },
        'conclusion': {
            'summary': '先完成第一层级排查再开始配方干预。',
            'managementDirections': [{'name': '配方方向', 'items': [{'code': 'p', 'text': '补充膳食纤维。'}]}],
        },
    }
    for code, content in overrides.items():
        contents[code] = content
    return {
        'status': 'success',
        'partialReasons': [],
        'data': {
            'version': 'assessment.v2',
            'introduction': {
                'title': '评估报告',
                'dataTiming': {'clinicalDate': '2026-06-30'},
                'preface': '本报告结合临床检查与肠道菌群检测。',
                'readingGuide': [{'name': '结构', 'text': '共六章。'}],
            },
            'chapters': [
                {'number': index, 'code': code, 'title': code, 'content': contents[code]}
                for index, code in enumerate(
                    ('overview', 'symptoms', 'clinical', 'microbiome', 'clusters', 'conclusion'), 1
                )
                if contents[code] is not None
            ],
        },
    }


def _request(rows=None):
    return {
        'userId': 'test-user',
        'sessionId': 'test-session',
        'age': 40,
        'sex': '男',
        'surveyList': [],
        'hospitalData': rows
        if rows is not None
        else [
            {'dataType': 'INSPECTION', 'itemName': '腰围', 'testResultValue': '92', 'unit': 'cm'},
            {
                'dataType': 'INSPECTION',
                'itemName': '甘油三酯(TG)',
                'testResultValue': '3.34',
                'referenceRange': '0-1.70',
            },
            {'dataType': 'INSPECTION', 'itemName': '肌酐', 'testResultValue': '82', 'referenceRange': '57-97'},
            {
                'dataType': 'INSPECTION',
                'itemName': '粪便隐血试验',
                'testResultValue': '阳性',
                'referenceRange': '阴性',
                'resultStatusName': '阳性',
            },
        ],
    }


def _chapters(body):
    return check.chapters_of(body)


@pytest.mark.parametrize(
    ('reference', 'sex', 'expected'),
    [
        ('3.89-6.11', None, (3.89, 6.11, None)),
        ('<34', None, (None, 34.0, None)),
        ('≥90', None, (90.0, None, None)),
        ('男性<90', '男', (None, 90.0, None)),
        ('-4.0-3.9', None, (-4.0, 3.9, None)),
        ('阴性', None, (None, None, '阴性')),
        ('男:40-50;女:35-45', '女', (35.0, 45.0, None)),
    ],
)
def test_parse_reference_reads_common_printed_forms(reference, sex, expected):
    assert check.parse_reference(reference, sex) == expected


def test_abnormal_rows_use_status_range_and_qualitative_result():
    rows = check.abnormal_rows(
        _request(
            [
                {
                    'dataType': 'INSPECTION',
                    'itemName': '钾(K)',
                    'testResultValue': '3.0',
                    'referenceRange': '3.50-5.30',
                },
                {'dataType': 'INSPECTION', 'itemName': 'TPOAb', 'testResultValue': '128', 'resultStatusName': '↑'},
                {
                    'dataType': 'INSPECTION',
                    'itemName': '粪便红细胞',
                    'testResultValue': '3-5',
                    'referenceRange': '未查见',
                },
                {
                    'dataType': 'INSPECTION',
                    'itemName': 'β-HCG',
                    'testResultValue': '<1.20',
                    'referenceRange': '未孕女性<5.0',
                },
                {
                    'dataType': 'INSPECTION',
                    'itemName': '尿蛋白',
                    'testResultValue': '阴性(-)',
                    'referenceRange': '阴性(-)',
                },
                {'dataType': 'EXAM', 'itemName': '主检结论：血脂增高', 'examResult': '低脂饮食。'},
            ]
        )
    )
    assert [(row['name'], row['reason']) for row in rows] == [
        ('钾(K)', 'range:low'),
        ('TPOAb', 'status:↑'),
        ('粪便红细胞', 'qualitative'),
    ]


def test_sections_accept_missing_data_sentences_and_report_empty_ones():
    body = _body(clusters={'clusters': [], 'lifestyleFactors': [], 'lifestyleSummary': '暂无可展示的解读'})
    result = check.check_sections(_chapters(body))
    assert result['empty'] == ['5.1 系统失衡集群定性与健康影响', '5.2 ATM 健康诱因复盘']
    assert '2.1 主诉症状谱与临床检验信号印证' not in result['empty']


def test_sections_list_entries_without_interpretation_and_missing_chapters():
    overview = _body()['data']['chapters'][0]['content']
    overview['systems'].append({'code': 'immune', 'name': '血液免疫', 'summary': None, 'status': '分项异常'})
    overview['systems'].append({'code': 'gut', 'name': '肠道', 'summary': None, 'level': 'notTested'})
    result = check.check_sections(_chapters(_body(overview=overview, conclusion=None)))
    assert result['emptyEntries'] == [{'path': 'overview.systems[1].summary', 'name': '血液免疫'}]
    assert result['missingChapters'] == ['第六章']
    assert '6.1 核心健康风险分层总结' in result['empty']


def test_words_count_forbidden_terms_in_visible_text_only():
    clinical = _body()['data']['chapters'][2]['content']
    clinical['clinicalGroups'][0]['summary'] = '按原件记录；您的甘油三酯偏高，仅供参考。'
    clinical['clinicalGroups'][0]['code'] = '模板-hidden'
    clinical['clinicalGroups'][0]['rows'][0]['question'] = '您最近有哪些不适？'
    words = check.check_words(_chapters(_body(clinical=clinical)))
    assert words['byTerm'] == {'原件': 1, '按原件': 1, '您': 1, '仅供参考': 1}
    assert {row['path'] for row in words['locations']} == {'clinical.clinicalGroups[0].summary'}


def test_abnormal_coverage_matches_abbreviation_and_lists_missing_rows():
    request = _request()
    request['hospitalData'].append(
        {
            'dataType': 'INSPECTION',
            'itemName': '癌胚抗原(CEA)',
            'testResultValue': '15.2',
            'referenceRange': '≤5.0',
            'resultStatusName': '偏高',
        }
    )
    coverage = check.check_abnormal_coverage(request, _chapters(_body()))
    assert coverage['abnormalCount'] == 3
    assert coverage['missing'] == ['癌胚抗原(CEA) 15.2']
    by_name = {item['name']: item for item in coverage['items']}
    assert by_name['甘油三酯(TG)']['match'] == 'name+value'
    assert by_name['粪便隐血试验']['inProse'] is True


def test_number_trace_flags_values_absent_from_sources():
    clinical = _body()['data']['chapters'][2]['content']
    clinical['clinicalGroups'][0]['summary'] = '甘油三酯3.34 mmol/L（参考0-1.70 mmol/L），血糖9.99 mmol/L。'
    trace = check.check_numbers(_request(), _chapters(_body(clinical=clinical)))
    assert [row['value'] for row in trace['missing']] == ['9.99 mmol/L']
    assert trace['byPool']['microbiome'] >= 1  # "53.34分" from the chapter-4 rows
    assert trace['checked'] == trace['found'] + 1


def test_p0_check_is_skipped_without_red_flag_module():
    result = check.check_p0(_request(), _chapters(_body()), 'tests_missing_red_flag_module')
    assert result['consistent'] is None
    assert result['redFlags']['available'] is False
    assert result['reportP0'] == ['粪便隐血阳性复查']


def test_p0_check_compares_red_flags_with_p0_items(monkeypatch):
    module = types.ModuleType('tests_fake_red_flags')

    def evaluate_red_flags(request):
        assert request.hospital_data  # receives the validated ReportRequest first
        return [{'itemName': '粪便隐血试验', 'department': '消化内科'}, {'itemName': '血钾', 'rule': '血钾<3.0'}]

    module.evaluate_red_flags = evaluate_red_flags
    monkeypatch.setitem(sys.modules, 'tests_fake_red_flags', module)
    result = check.check_p0(_request(), _chapters(_body()), 'tests_fake_red_flags')
    assert result['redFlags']['available'] is True
    assert result['unreflectedHits'] == ['血钾 血钾<3.0']
    assert result['p0WithoutRedFlag'] == []
    assert result['consistent'] is False


def test_length_skips_renderer_duplicates():
    length = check.check_length(_chapters(_body()))
    assert length['tableRows']['byChapter']['第一章'] == 1  # bodyMetrics duplicates bodyGroups rows
    assert length['tableRows']['byChapter']['第四章'] == 1  # structure duplicates structureGroups rows
    assert length['hanzi']['total'] > 0


def test_main_writes_json_reads_audit_and_fails_in_strict_mode(tmp_path):
    clinical = _body()['data']['chapters'][2]['content']
    clinical['summary'] = '原报告显示甘油三酯升高。'
    (tmp_path / 'callback_v2.json').write_text(json.dumps(_body(clinical=clinical)), encoding='utf-8')
    (tmp_path / 'audit.json').write_text(json.dumps({'elapsedSeconds': 1900.5}), encoding='utf-8')
    request = tmp_path / 'request.json'
    request.write_text(json.dumps(_request()), encoding='utf-8')
    output = tmp_path / 'check.json'
    code = check.main([str(tmp_path), '--request', str(request), '--output', str(output), '--strict'])
    result = json.loads(output.read_text(encoding='utf-8'))
    assert code == 1
    assert result['status']['elapsedSeconds'] == 1900.5
    assert result['status']['overTimeLimit'] is True
    assert result['words']['byTerm'] == {'原报告': 1}
    assert any('禁用词' in failure for failure in result['failures'])


def test_local_callback_is_converted_when_no_frontend_body(tmp_path, monkeypatch):
    from modules.report import evaluation_callback

    (tmp_path / 'callback.json').write_text(json.dumps({'status': 'success', 'data': {}}), encoding='utf-8')
    monkeypatch.setattr(evaluation_callback, 'build_frontend_evaluation_callback', lambda payload: _body())
    body, source = check.load_body(tmp_path)
    assert source == 'callback.json (converted)'
    assert body['data']['version'] == 'assessment.v2'


def test_request_fact_rows_feed_the_real_rule_table():
    request = {
        'sex': '男',
        'age': 50,
        'hospitalData': [
            {
                'itemName': '粪便隐血试验',
                'testResultValue': '阳性',
                'resultStatusName': '阳性',
                'referenceRange': '阴性',
            },
            {'itemName': '血红蛋白', 'testResultValue': '150', 'unit': 'g/L', 'referenceRange': '130-175'},
        ],
    }
    rows = check.request_fact_rows(request)
    assert [row['factId'] for row in rows] == ['R1', 'R2']
    assert all(row['kind'] == 'clinical' for row in rows)
    flags = check.red_flag_hits(request)
    assert flags['available'] is True
    assert any('隐血' in hit for hit in flags['hits'])
