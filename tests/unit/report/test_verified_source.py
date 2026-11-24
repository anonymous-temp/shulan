"""Partial Report projection accepts source-bound identity and verified scalars."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from adapters.report.verified_source import trusted_report_source
from modules.report import parsing


TABLE_CHECKSUM = 'a' * 64


def outcome(*rows):
    return SimpleNamespace(report={'observations': list(rows)})


def test_chinese_only_microbe_keeps_classification_without_inventing_latin_identity():
    raw = {'sourceName': 'synthetic.pdf', 'taxa': [{
        'taxonId': 'example', 'latinName': 'Unverified latin', 'chineseName': '原文菌属',
        'value': '0.2', 'unit': '%', 'category': 'other',
    }]}
    base = outcome({'itemName': '原文菌属', 'resultValue': '0.2', 'unit': '%', 'flag': '偏低'})
    verified = trusted_report_source(raw, [base])
    assert verified['taxa'] == []
    assert verified['sourceMicrobiomeTerms'] == ['原文菌属']


def test_source_projection_keeps_document_kind_for_microbiome_function_values():
    raw = {'sourceName': 'microbial.pdf', 'supplementaryHospitalData': [
        {'dataType': 'INSPECTION', 'itemName': '营养功能指数', 'testResultValue': '85'}]}
    base = SimpleNamespace(report={'reportType': 'microbiome', 'sections': [
        {'observations': [{'kind': 'measurement', 'itemName': '营养功能指数', 'resultValue': '85'}]}]})
    rows = trusted_report_source(raw, [base])['supplementaryHospitalData']
    assert len(rows) == 1
    assert rows[0]['sourceReportType'] == 'microbiome'
    assert rows[0]['sourceFile'] == 'microbial.pdf'
    assert rows[0]['testResultValue'] == '85'


def metric(value='1.00', unit='mg/L', name='指标甲'):
    return {'code': 'example', 'name': name, 'value': value, 'unit': unit, 'status': '异常', 'referenceRange': 'raw'}


def source(row):
    return {'sourceName': 'synthetic.pdf', 'metrics': [row], 'sourceSummary': '不可信摘要'}


def observation(value='1', unit='mg/L', **changes):
    return {'itemName': '指标甲', 'resultValue': value, 'unit': unit, 'flag': None, **changes}


def test_exact_value_projection_uses_only_verified_metadata_and_drops_raw_ancillary_facts():
    raw = source(metric())
    raw.update(
        subject={'name': '原文未确认姓名'},
        sample={'reportDate': '2020-01-01'},
        enterotype='未知肠型',
        panelCounts=[{'rowCount': 999}],
        supplementaryHospitalData=[{'itemName': '探针', 'testResultValue': '999'}],
    )
    original = deepcopy(raw)
    verified = trusted_report_source(raw, [outcome(observation(observedAt='2026-09-01'))])
    assert verified['metrics'] == [
        {'code': 'example', 'name': '指标甲', 'value': '1', 'unit': 'mg/L', 'referenceRange': None, 'status': None}
    ]
    assert verified['sample'] == {'reportDate': '2026-09-01'}
    assert verified['subject'] == {'name': '原文未确认姓名'}
    assert verified['sourceSummary'] is None and verified['enterotype'] is None
    assert not verified['panelCounts'] and not verified['supplementaryHospitalData']
    assert raw == original


def test_partial_projection_keeps_only_validated_source_subject_scalars():
    raw = source(metric())
    raw['subject'] = {'name': '许先生', 'age': 48, 'sex': '男'}
    verified = trusted_report_source(
        raw,
        [outcome(observation(observedAt='2026-09-01'))],
    )
    assert verified['subject'] == {'name': '许先生', 'age': 48, 'sex': '男'}

    raw['subject'] = {'name': '受检者信息', 'age': 999, 'sex': '其他'}
    assert trusted_report_source(raw, [outcome(observation())])['subject'] == {}


@pytest.mark.parametrize(
    'candidate,base',
    [
        (metric('1'), observation('<1')),
        (metric('<1'), observation('1')),
        (metric('1%', None), observation('1', None)),
        (metric('1', None), observation('1', '%')),
        (metric('1', 'mg/L'), observation('1', 'mg/dL')),
        (metric('1', name='指标甲说明'), observation()),
        (metric('1↑'), observation()),
        (metric('999'), observation()),
    ],
)
def test_comparison_units_arrows_substrings_and_unmatched_values_cannot_be_merged(candidate, base):
    assert trusted_report_source(source(candidate), [outcome(base)])['metrics'] == []


def test_printed_percentage_and_confirmed_arrow_can_match_without_converting_units():
    verified = trusted_report_source(source(metric('1.00%↑', None)), [outcome(observation('1', '%', flag='偏高'))])
    assert verified['metrics'][0]['value'] == '1'
    assert verified['metrics'][0]['unit'] == '%'
    assert verified['metrics'][0]['status'] == '偏高'


def test_different_dates_make_identical_names_and_values_ambiguous():
    rows = [observation(observedAt=date) for date in ('2026-08-01', '2026-09-01')]
    assert trusted_report_source(source(metric()), [outcome(*rows)])['metrics'] == []
    assert len(trusted_report_source(source(metric()), [outcome(rows[0], deepcopy(rows[0]))])['metrics']) == 1


def test_explicit_bilingual_taxon_identity_preserves_verified_value_but_not_raw_category_or_description():
    raw = {
        'sourceName': 'microbiome.pdf',
        'taxa': [
            {
                'taxonId': 'taxon-a',
                'category': 'pathogenic',
                'latinName': 'Example species',
                'chineseName': '示例菌',
                'value': '1.00',
                'unit': '%',
                'referenceRange': '0',
                'explicitlyAbnormal': True,
                'detectionStatus': 'detected',
            }
        ],
        'diseaseResults': [
            {
                'diseaseId': 'risk-a',
                'name': '风险甲',
                'sourceScore': '2.0',
                'sourceLevel': '高风险',
                'sourceDescription': '原文推断的描述',
                'relatedTaxa': ['未经证实菌'],
            }
        ],
    }
    verified = trusted_report_source(
        raw,
        [
            outcome(
                observation('1', '%', itemName='示例菌（Example species）'),
                observation('2', None, itemName='风险甲', flag='正常'),
            )
        ],
    )
    assert verified['taxa'][0]['category'] == 'other'
    assert verified['taxa'][0]['referenceRange'] is None
    assert verified['taxa'][0]['sourceStatus'] is None
    assert verified['taxa'][0]['detectionStatus'] == 'unknown'
    assert verified['diseaseResults'][0]['sourceLevel'] == '正常'
    assert verified['diseaseResults'][0]['sourceDescription'] is None
    assert not verified['diseaseResults'][0]['relatedTaxa']
    # An unverified Latin alias must not inherit a Chinese-only identity match.
    verified = trusted_report_source(raw, [outcome(observation('1', '%', itemName='示例菌', observedAt='2026-09-01'))])
    assert not verified['taxa']
    assert verified['sample'] == {}


def printed_source():
    document = parsing.ExtractedDocument('original.pdf', 'pdf', '', tables=[
        parsing.ExtractedTable(1, [
            ['中文名称', '拉丁学名', '检测结果', '结果评价', '参考范围(%)'],
            ['测试未知菌', 'Unlisted example', '0.12', '超标', '0-0.1'],
        ], context='其他菌'),
        parsing.ExtractedTable(2, [
            ['疾病名称', '风险指数', '风险等级', '正负相关菌'],
            ['测试疾病', '55', '中风险', '测试未知菌(超标)'],
        ]),
    ], metadata={'content_sha256': TABLE_CHECKSUM})
    data = {
        'sourceName': document.filename,
        'sourceChecksum': TABLE_CHECKSUM,
        'taxa': [item.model_dump(mode='json', by_alias=True) for item in parsing._document_taxa(document)],
        'diseaseResults': [item.model_dump(mode='json', by_alias=True) for item in parsing._document_diseases(document)],
    }
    assert len(data['taxa']) == len(data['diseaseResults']) == 1
    return document, data


def test_original_table_restores_bilingual_identity_direction_and_disease_relation():
    document, data = printed_source()
    data['taxa'][0]['category'] = 'pathogenic'  # Must use the printed table, not this override.
    data['diseaseResults'][0]['relatedTaxa'] = ['编造菌']
    verified = trusted_report_source(data, [outcome(
        observation('0.12', None, itemName='测试未知菌'),
        observation('55', None, itemName='测试疾病'),
    )], table_documents=[document])
    assert verified['taxa'][0]['latinName'] == 'Unlisted example'
    assert verified['taxa'][0]['category'] == 'other'
    assert verified['taxa'][0]['unit'] == '%'
    assert verified['taxa'][0]['sourceStatus'] == '超标'
    assert verified['taxa'][0]['referenceRange'] == '0-0.1'
    assert verified['taxa'][0]['explicitlyAbnormal'] is True
    assert verified['diseaseResults'][0]['relatedTaxa'] == ['测试未知菌(超标)']
    assert verified['diseaseResults'][0]['sourceLevel'] == '中风险'
    assert verified['sourceSummary'] is None


def test_original_table_survives_missing_or_conflicting_ai_projection():
    document, data = printed_source()
    verified = trusted_report_source(data, [], table_documents=[document])
    assert len(verified['taxa']) == len(verified['diseaseResults']) == 1
    for change in ({'value': '999'}, {'unit': 'mg/L'}, {'latinName': 'Another species'}):
        changed = deepcopy(data)
        changed['taxa'][0].update(change)
        projected = trusted_report_source(changed, [outcome(
            observation('0.12', '%', itemName='测试未知菌'),
        )], table_documents=[document])['taxa']
        assert [(item['latinName'], item['value']) for item in projected] == [
            ('Unlisted example', '0.12')
        ]


@pytest.mark.parametrize('checksum', [None, 'b' * 64])
def test_original_table_requires_matching_checksum(checksum):
    document, data = printed_source()
    data['sourceChecksum'] = checksum
    verified = trusted_report_source(data, [], table_documents=[document])
    assert not verified['taxa']
    assert not verified['diseaseResults']


def test_table_collapses_incomplete_mirrors_and_outweighs_conflicting_ai_metadata():
    document, data = printed_source()
    rows = [
        observation('0.12↑', '%', itemName='测试未知菌', flag='偏高', observedAt='2026-09-01'),
        observation('0.12', None, itemName='测试未知菌', observedAt='2026-09-01'),
    ]
    assert len(trusted_report_source(data, [outcome(*rows)], table_documents=[document])['taxa']) == 1
    for change in ({'observedAt': '2026-08-01'}, {'flag': '偏低'}, {'referenceRange': '9-10'}):
        conflicting = deepcopy(rows)
        conflicting[0]['referenceRange'] = '0-0.1'
        conflicting[1].update(change)
        projected = trusted_report_source(data, [outcome(*conflicting)], table_documents=[document])['taxa']
        assert [(item['latinName'], item['value']) for item in projected] == [
            ('Unlisted example', '0.12')
        ]


def project_shared_rows(rows, *, text='', report_type='microbiome'):
    from modules.analysis.application.output_profiles import DocumentSnapshot
    from modules.report.document_source import extract_intrinsic_report_source

    report = {'reportType': report_type, 'observations': rows, 'aiSummary': '不可当作来源事实'}
    raw = extract_intrinsic_report_source((DocumentSnapshot('d', report, text, 'sample.png', 'png', 'sha'),))
    return trusted_report_source(raw, [SimpleNamespace(report=report)])


def test_shared_score_and_panel_results_survive_parser_default_units_and_names():
    rows = [
        observation('58', None, itemName='肠道总评分', kind='assessment'),
        observation('5.4840%', '%', itemName='有益菌', kind='measurement'),
        observation('5.2454%', '%', itemName='有害菌', kind='measurement'),
    ]
    result = project_shared_rows(rows, text='肠道总体评分\n您的肠道总评分为：58\n有益菌 5.4840%\n有害菌 5.2454%')
    metrics = {item['code']: item for item in result['metrics']}
    assert set(metrics) == {'gut_overall_score', 'panel_beneficial', 'panel_harmful'}
    assert metrics['gut_overall_score']['value'] == '58'
    assert metrics['gut_overall_score']['unit'] is None  # Never invent the missing unit.
    assert metrics['panel_beneficial']['unit'] == '%'
    assert all(item['status'] is None for item in metrics.values())
    assert not result['diseaseResults'] and not result['taxa']


@pytest.mark.parametrize('unit', ['%', 'CFU/g stool', None])
def test_unlisted_scientific_taxon_uses_shared_measurement_without_category_dictionary(unit):
    result = project_shared_rows([
        observation('2.4E8', unit, itemName='Unlisted organism', kind='measurement'),
    ], text='Scientific name | Result\nUnlisted organism | 2.4E8')
    assert len(result['taxa']) == 1
    taxon = result['taxa'][0]
    assert (taxon['latinName'], taxon['value'], taxon['unit']) == ('Unlisted organism', '2.4E8', unit)
    assert taxon['category'] == 'other' and not taxon['explicitlyAbnormal']
    assert not result['supplementaryHospitalData'] and not result['diseaseResults']


def test_metabolite_measurements_in_microbiome_report_are_not_synthesis_scores_or_taxa():
    result = project_shared_rows([
        observation('0.7', 'mg/g', itemName='Triglycerides', kind='measurement'),
        observation('6.7', 'micromol/g', itemName='乙酸', kind='measurement'),
        observation('683', 'mcg/mL', itemName='Fecal secretory IgA', kind='measurement'),
        observation('5', None, itemName='Total bacteria', kind='measurement'),
    ])
    assert not result['metrics'] and not result['taxa']
    rows = {row['itemName']: row for row in result['supplementaryHospitalData']}
    assert rows['Triglycerides']['testResultValue'] == '0.7 mg/g'
    assert rows['乙酸']['testResultValue'] == '6.7 micromol/g'
    assert rows['Fecal secretory IgA']['testResultValue'] == '683 mcg/mL'
    assert rows['Total bacteria']['testResultValue'] == '5'


def test_scale_legend_and_unbound_disease_prose_are_not_measurements():
    result = project_shared_rows([
        observation('LOW INHIBITION    HIGH INHIBITION', None, itemName='Unlisted organism', kind='measurement'),
        observation('可能增加某疾病风险', None, itemName='Unlisted organism', kind='assessment'),
    ])
    assert not any(result[key] for key in ('taxa', 'metrics', 'diseaseResults', 'supplementaryHospitalData'))


def test_exact_clinical_mirrors_collapse_but_different_dates_do_not():
    row = observation('1', 'mg/L', kind='measurement', observedAt='2026-09-01')
    result = project_shared_rows([row, deepcopy(row)], report_type='lab')
    assert len(result['supplementaryHospitalData']) == 1
    later = {**row, 'observedAt': '2026-09-02'}
    assert not project_shared_rows([row, later], report_type='lab')['supplementaryHospitalData']


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('report_type', ['microbiome', 'lab'])
def test_numeric_source_kind_mirrors_keep_one_conservative_literal_result(reverse, report_type):
    item = observation('73', None, itemName='未收录来源数值项目', kind='assessment', observedAt='2026-09-01')
    rows = [item, {**item, 'kind':'measurement'}]
    if reverse:
        rows.reverse()
    report = SimpleNamespace(report={'reportType':report_type, 'observations':rows})
    raw = {'sourceName':'sample.png', 'supplementaryHospitalData':[
        {'itemName':item['itemName'], 'dataType':'INSPECTION', 'testResultValue':'73'}]}
    result = trusted_report_source(raw, [report])['supplementaryHospitalData']
    assert len(result) == 1
    assert result[0]['dataType'] == 'EXAM' and result[0]['examResult'] == '73'
    assert result[0]['sourceReportType'] == report_type
    assert result[0]['reportTime'] == '2026-09-01'
    if report_type == 'microbiome':
        from modules.report.schemas import ParsedMicrobiomeReport, ReportRequest
        from modules.report.clinical_report import collect_source_facts
        doc = collect_source_facts(ReportRequest(userId='u', sessionId='s', hospitalData=result),
            ParsedMicrobiomeReport(sourceType='pending', sourceName='sample.png', subject={}))
        assert len(doc.facts) == 1 and doc.facts[0].kind == 'function'


@pytest.mark.parametrize('change', [
    {'observedAt':'2026-09-02'}, {'flag':'偏高'}, {'referenceRange':'10-20'},
    {'sourceReportType':'lab'}, {'kind':'narrative'},
])
def test_numeric_kind_mirrors_cannot_hide_other_source_metadata_conflicts(change):
    item = observation('73', None, itemName='未收录来源数值项目', kind='assessment', observedAt='2026-09-01')
    other = {**item, 'kind':'measurement'}
    report_type = change.get('sourceReportType', 'microbiome')
    other.update({k:v for k,v in change.items() if k != 'sourceReportType'})
    report = SimpleNamespace(report={'reportType':'microbiome', 'observations':[item],
        'sections':[{'reportType':report_type, 'observations':[other]}]})
    raw = {'sourceName':'sample.png', 'supplementaryHospitalData':[
        {'itemName':item['itemName'], 'dataType':'INSPECTION', 'testResultValue':'73'}]}
    assert not trusted_report_source(raw, [report])['supplementaryHospitalData']


def test_non_numeric_kind_disagreement_is_not_merged():
    item = observation('原文结论', None, itemName='查体项目', kind='assessment')
    raw = {'sourceName':'sample.pdf', 'supplementaryHospitalData':[
        {'itemName':item['itemName'], 'dataType':'EXAM', 'examResult':item['resultValue']}]}
    assert not trusted_report_source(raw, [outcome(item, {**item,'kind':'measurement'})])['supplementaryHospitalData']


def test_source_bound_checkup_assessment_is_not_discarded_as_model_interpretation():
    row = observation('来源记录的某项异常，需复核', None, itemName='查体结论', kind='assessment',
        evidence={'sourceId': 'line-1', 'quote': '查体结论：来源记录的某项异常，需复核'})
    result = project_shared_rows([row], report_type='checkup')
    assert result['supplementaryHospitalData'][0]['examResult'] == row['resultValue']
    assert result['supplementaryHospitalData'][0]['dataType'] == 'EXAM'
    assert not project_shared_rows([row], report_type='microbiome')['supplementaryHospitalData']
    unbound = {**row, 'evidence': {'quote': '完全不同的来源内容'}}
    assert not project_shared_rows([unbound], report_type='checkup')['supplementaryHospitalData']


@pytest.mark.parametrize('report_type', ['checkup', 'imaging', 'lab'])
@pytest.mark.parametrize('file_type', ['pdf', 'png'])
def test_public_snapshot_keeps_accepted_assessment_after_private_evidence_is_removed(report_type, file_type):
    from modules.analysis.application.output_profiles import DocumentSnapshot
    from modules.report.document_source import extract_intrinsic_report_source
    from utils.report_content import report_public_content

    row = observation('来源记录的异常，需复核', None, itemName='查体所见', kind='assessment',
        observedAt='2026-09-01', evidence={'sourceId':'line-1', 'quote':'查体所见：来源记录的异常，需复核'})
    accepted = {'reportType':report_type, 'observations':[row],
        'processing':{'status':'partial', 'retryable':True},
        'unresolvedItems':[observation('未确认的另一异常', None, itemName='未确认项目', kind='assessment')],
        'aiSummary':'模型生成的疾病推断'}
    public = report_public_content(accepted)
    assert 'evidence' not in public['observations'][0]
    results = []
    for report in (accepted, public):
        snapshot = DocumentSnapshot('d', report, '', 'sample.'+file_type, file_type, 'sha')
        raw = extract_intrinsic_report_source((snapshot,))
        projected = trusted_report_source(raw, [SimpleNamespace(report=report)])
        results.append(projected['supplementaryHospitalData'])
    assert results[0] == results[1]
    assert len(results[1]) == 1
    assert results[1][0]['examResult'] == row['resultValue']
    assert results[1][0]['reportTime'] == row['observedAt']
    assert results[1][0]['dataType'] == 'EXAM'


def test_public_assessment_trust_is_not_applied_to_untrusted_rows_or_microbiome_inference():
    from modules.report.document_source import _clinical_rows, extract_intrinsic_report_source
    from modules.analysis.application.output_profiles import DocumentSnapshot
    row = observation('待复核异常', None, itemName='查体所见', kind='assessment')
    assert not _clinical_rows({'reportType':'checkup', 'observations':[row]}, 'sample.pdf')
    assert not project_shared_rows([row], report_type='microbiome')['supplementaryHospitalData']
    report = {'reportType':'mixed', 'sections':[
        {'reportType':'checkup', 'observations':[row]},
        {'reportType':'microbiome', 'observations':[{**row, 'itemName':'菌群疾病推断'}]},
    ]}
    raw = extract_intrinsic_report_source((DocumentSnapshot('d', report, '', 'sample.png', 'png', 'sha'),))
    result = trusted_report_source(raw, [SimpleNamespace(report=report)])
    assert [r['itemName'] for r in result['supplementaryHospitalData']] == ['查体所见']


@pytest.mark.parametrize('file_type', ['pdf', 'png'])
@pytest.mark.parametrize('private_quote', [False, True])
def test_source_numeric_microbiome_assessment_survives_as_function_not_diagnosis(file_type, private_quote):
    from modules.analysis.application.output_profiles import DocumentSnapshot
    from modules.report.document_source import extract_intrinsic_report_source
    from modules.report.clinical_report import collect_source_facts
    from modules.report.schemas import ParsedMicrobiomeReport, ReportRequest

    row = observation('73', None, itemName='未收录功能模型结果', kind='assessment',
        referenceRange='20-60', flag='偏高', observedAt='2026-09-03')
    if private_quote:
        row['evidence'] = {'quote': '未收录功能模型结果 73 偏高 20-60'}
    report = {'reportType': 'microbiome', 'observations': [row],
        'processing': {'status': 'partial'}, 'aiSummary': '模型建议确诊某疾病',
        'unresolvedItems': [observation('99', None, itemName='未确认功能值', kind='assessment')]}
    raw = extract_intrinsic_report_source((DocumentSnapshot(
        'source', report, '', 'source.'+file_type, file_type, 'checksum'),))
    projected = trusted_report_source(raw, [SimpleNamespace(report=report)])
    rows = projected['supplementaryHospitalData']
    assert len(rows) == 1
    assert rows[0]['itemName'] == row['itemName']
    assert rows[0]['examResult'] == '73'
    assert rows[0]['reportTime'] == row['observedAt']
    assert rows[0]['referenceRange'] == '20-60'
    assert rows[0]['resultStatusName'] == '偏高'
    parsed = ParsedMicrobiomeReport.model_validate(projected)
    document = collect_source_facts(ReportRequest(userId='u', sessionId='s', hospitalData=rows), parsed)
    assert len(document.facts) == 1 and document.facts[0].kind == 'function'
    assert not projected['diseaseResults']
    assert report['processing']['status'] == 'partial'


def test_nested_numeric_microbiome_assessment_inherits_source_type():
    from modules.analysis.application.output_profiles import DocumentSnapshot
    from modules.report.document_source import extract_intrinsic_report_source
    row = observation('73', None, itemName='另一个模型指数', kind='assessment')
    report = {'reportType': 'microbiome', 'sections': [{'observations': [row]}]}
    raw = extract_intrinsic_report_source((DocumentSnapshot('d', report, '', 'source.png', 'png', 'sha'),))
    rows = trusted_report_source(raw, [SimpleNamespace(report=report)])['supplementaryHospitalData']
    assert len(rows) == 1 and rows[0]['sourceReportType'] == 'microbiome'


def test_numeric_function_trust_does_not_extend_to_unverified_or_narrative_assessments():
    from modules.report.document_source import _clinical_rows
    row = observation('73', None, itemName='某功能模型值', kind='assessment')
    assert not _clinical_rows({'reportType': 'microbiome', 'observations': [row]}, 'source.pdf')
    for value in ('未检出', '可能导致某疾病', '73提示需要服用某药'):
        result = project_shared_rows([{**row, 'resultValue': value}])
        assert not result['supplementaryHospitalData']


@pytest.mark.parametrize('text,expected', [
    ('报告⽣成：2026-09-09', '2026-09-09'),
    ('报告生成日期：2026年9月9日', '2026年9月9日'),
    ('Report date:\n2026/09/09', '2026/09/09'),
    ('报告生成：2026-02-30', None),
    ('采样日期：2026-09-09', None),
    ('参考范围：2026-09-09', None),
    ('报告日期：2026-08-01\n报告生成：2026-09-09', None),
    ('报告日期：2026-09-09\n报告生成：2026/9/9', '2026-09-09'),
])
def test_explicit_original_report_date_is_validated_without_sample_date_substitution(text, expected):
    document = parsing.ExtractedDocument('source.png', 'image', text)
    assert parsing._explicit_report_date(document) == expected


def test_partial_projection_recovers_report_date_only_from_original_not_candidate_metadata():
    document = parsing.ExtractedDocument('source.pdf', 'pdf', '报告生成：2026-09-09')
    data = source(metric())
    data['sample'] = {'reportDate': '2026-09-20'}  # Candidate metadata is not authority.
    rows = [outcome(observation())]
    result = trusted_report_source(data, rows, table_documents=[document])
    assert result['sample'] == {'reportDate': '2026-09-09'}
    assert trusted_report_source(data, rows)['sample'] == {}
    # A conflicting accepted date must never be overwritten by the header.
    dated = outcome(observation(observedAt='2026-09-08'))
    assert trusted_report_source(data, [dated], table_documents=[document])['sample'] == {}


def test_metric_alias_never_relaxes_unit_or_value_identity():
    raw = source({**metric('1', '%', '有益菌总检出丰度'), 'code': 'panel_beneficial'})
    for row in (observation('1', 'mg/g', itemName='有益菌'), observation('2', '%', itemName='有益菌')):
        assert not trusted_report_source(raw, [outcome(row)])['metrics']


def test_organism_count_is_not_percentage_panel_abundance():
    result = project_shared_rows([observation('12', '种', itemName='有益菌', kind='measurement')])
    assert not result['metrics']
    assert result['supplementaryHospitalData'][0]['testResultValue'] == '12 种'


def test_english_measurement_without_taxonomic_source_is_not_a_latin_organism():
    result = project_shared_rows([
        observation('500', None, itemName='Fecal elastase', kind='measurement'),
    ], text='Digestion\nFecal elastase 500')
    assert not result['taxa']
    assert result['supplementaryHospitalData'][0]['testResultValue'] == '500'


def test_formatted_measurement_with_unit_is_not_lost_or_reinterpreted():
    result = project_shared_rows([
        observation('1,547', 'U/g', itemName='示例酶活性', kind='measurement'),
    ])
    assert result['supplementaryHospitalData'][0]['testResultValue'] == '1,547 U/g'
    raw = source(metric('1,547 U/g', None))
    assert len(trusted_report_source(raw, [outcome(observation('1,547', 'U/g'))])['metrics']) == 1
    for base in (observation('1547', 'U/g'), observation('1.547', 'U/g'), observation('1,547', 'U/L')):
        assert not trusted_report_source(raw, [outcome(base)])['metrics']


@pytest.mark.parametrize('unknown', [None, '', 'unknown'])
@pytest.mark.parametrize('reverse', [False, True])
def test_unknown_section_mirror_does_not_hide_an_identical_known_source(unknown, reverse):
    row = observation('0.7', None, itemName='某功能检测', kind='measurement', observedAt='2026-01-01')
    raw = {'sourceName': 'source.pdf', 'supplementaryHospitalData': [
        {'dataType': 'INSPECTION', 'itemName': '某功能检测', 'testResultValue': '0.7'}]}
    sections = [{'reportType': kind, 'observations': [deepcopy(row)]} for kind in ('microbiome', unknown)]
    if reverse:
        sections.reverse()
    base = SimpleNamespace(report={'sections': sections})
    result = trusted_report_source(raw, [base])['supplementaryHospitalData']
    assert len(result) == 1
    assert result[0]['sourceReportType'] == 'microbiome'


@pytest.mark.parametrize('change', [
    {'observedAt': '2026-01-02'}, {'unit': '%'}, {'referenceRange': '8-9'}, {'flag': '偏高'},
])
def test_unknown_mirror_cannot_erase_conflicting_source_metadata(change):
    first = observation('0.7', None, kind='measurement', observedAt='2026-01-01')
    second = {**first, **change}
    base = SimpleNamespace(report={'reportType': 'microbiome', 'observations': [first],
        'sections': [{'reportType': 'unknown', 'observations': [second]}]})
    result = trusted_report_source(source(metric('0.7', None)), [base])
    # A different unit is not a competing match; otherwise genuine metadata
    # conflicts must not be resolved by preferring the typed parent.
    assert len(result['metrics']) == (1 if 'unit' in change else 0)


def test_known_clinical_and_microbial_types_remain_distinct():
    row = observation('1', None, kind='measurement')
    base = SimpleNamespace(report={'reportType': 'microbiome', 'observations': [row],
        'sections': [{'reportType': 'lab', 'observations': [deepcopy(row)]}]})
    assert not trusted_report_source(source(metric('1', None)), [base])['metrics']


def unclassified_taxon_table(source_type='pdf', **changes):
    from modules.report.parsing import ExtractedDocument, ExtractedTable
    rows = changes.pop('rows', [
        ['拉丁学名', '中文名称', '检测结果', '参考范围', '是否异常'],
        ['Novelaceae', '-', '0.1', '0.2-0.8', '缺乏'],
        ['Newgenus', '-', '0.3', '0-0.8', '正常'],
        ['Unlisted example', '来源新菌', '0.2', '0-0.1', ''],
    ])
    return ExtractedDocument('source.' + source_type, source_type, '', tables=[
        ExtractedTable(page_number=1, context='过敏风险评估', rows=rows),
    ], metadata={'content_sha256': TABLE_CHECKSUM}, **changes)


@pytest.mark.parametrize('source_type', ['pdf', 'image'])
def test_printed_taxa_outside_named_panels_preserve_source_not_disease_inference(source_type):
    from modules.report.document_source import printed_table_taxa
    doc = unclassified_taxon_table(source_type)
    taxa = printed_table_taxa(doc)
    assert [(t.latin_name, t.value, t.source_status, t.explicitly_abnormal) for t in taxa] == [
        ('Novelaceae', '0.1', '缺乏', True), ('Newgenus', '0.3', '正常', False),
        ('Unlisted example', '0.2', None, False),  # No inferred flag from range.
    ]
    assert all(t.category == 'other' and t.unit is None for t in taxa)
    result = trusted_report_source(
        {'sourceName': doc.filename, 'sourceChecksum': TABLE_CHECKSUM},
        [outcome()], table_documents=[doc],
    )
    assert len(result['taxa']) == 3
    assert not result['diseaseResults']


def test_printed_taxon_repeats_are_deduplicated_without_selecting_conflicting_rows():
    from modules.report.document_source import printed_table_taxa
    doc = unclassified_taxon_table()
    doc.tables.append(deepcopy(doc.tables[0]))
    assert len(printed_table_taxa(doc)) == 3
    doc.tables[1].rows[1][2] = '0.9'
    assert {t.latin_name for t in printed_table_taxa(doc)} == {'Newgenus', 'Unlisted example'}


@pytest.mark.parametrize('header', [
    ['指标', '中文名称', '检测结果', '参考范围', '是否异常'],
    ['拉丁学名', '中文名称', '风险指数', '参考范围', '风险等级'],
    ['相关菌', '相关疾病', '相关系数'],
])
def test_non_taxonomic_or_risk_tables_are_not_promoted_to_source_taxa(header):
    from modules.report.document_source import printed_table_taxa
    doc = unclassified_taxon_table(rows=[header, ['Novelaceae', '-', '0.1', '0.2-0.8', '高风险']])
    assert printed_table_taxa(doc) == []


def test_printed_table_unit_is_only_used_when_explicitly_provided():
    from modules.report.document_source import printed_table_taxa
    doc = unclassified_taxon_table()
    doc.tables[0].rows[0][2] = '检测结果(%)'
    assert all(t.unit == '%' for t in printed_table_taxa(doc))


@pytest.mark.parametrize('reverse', [False, True])
def test_printed_taxon_missing_unit_mirror_reuses_only_explicit_matching_unit(reverse):
    from modules.report.document_source import printed_table_taxa
    doc = unclassified_taxon_table()
    detail = deepcopy(doc.tables[0])
    detail.rows[0][2] = '检测结果(%)'
    doc.tables.append(detail)
    if reverse:
        doc.tables.reverse()
    taxa = printed_table_taxa(doc)
    assert len(taxa) == 3
    assert all(t.unit == '%' for t in taxa)
    assert taxa[0].explicitly_abnormal


def test_printed_taxon_explicit_unit_conflict_is_not_resolved_by_first_row():
    from modules.report.document_source import printed_table_taxa
    doc = unclassified_taxon_table()
    doc.tables[0].rows[0].append('单位')
    for row in doc.tables[0].rows[1:]:
        row.append('%')
    detail = deepcopy(doc.tables[0])
    detail.rows[1][-1] = 'CFU/g'
    doc.tables.append(detail)
    assert {t.latin_name for t in printed_table_taxa(doc)} == {'Newgenus', 'Unlisted example'}


def test_printed_taxon_is_not_a_function_or_clinical_problem_in_final_fact_inventory():
    from modules.report.clinical_report import collect_source_facts
    from modules.report.schemas import ParsedMicrobiomeReport, ReportRequest
    doc = unclassified_taxon_table()
    raw = {
        'sourceType': 'pdf', 'sourceName': doc.filename, 'sourceChecksum': TABLE_CHECKSUM,
        'supplementaryHospitalData': [
            {'dataType': 'INSPECTION', 'itemName': 'Novelaceae', 'testResultValue': '0.1'},
        ],
    }
    result = trusted_report_source(raw, [SimpleNamespace(report={'reportType': 'microbiome', 'observations': [
        observation('0.1', None, itemName='Novelaceae', kind='measurement'),
    ]})], table_documents=[doc])
    parsed = ParsedMicrobiomeReport.model_validate(result)
    request = ReportRequest(userId='synthetic', sessionId='source-kind', hospitalData=parsed.supplementary_hospital_data)
    facts = collect_source_facts(request, parsed).facts
    assert [f.kind for f in facts if f.name == 'Novelaceae'] == ['context', 'microbe']
    assert not any(f.kind in {'clinical', 'function'} for f in facts)


@pytest.mark.parametrize('version_field', ['EXTRACTOR_VERSION', 'REPORT_CONTENT_REVISION'])
def test_report_generation_revision_invalidates_report_reuse_without_changing_request(monkeypatch, version_field):
    from modules.report import task_coordinator
    from modules.report.schemas import ReportRequest
    request = ReportRequest(userId='synthetic', sessionId='source-version')
    original = request.model_dump(mode='json', by_alias=True)
    first = task_coordinator.report_request_fingerprint(request)
    assert task_coordinator.report_request_fingerprint(ReportRequest.model_validate(original)) == first
    monkeypatch.setattr(task_coordinator, version_field, 'next-synthetic-version')
    second = task_coordinator.report_request_fingerprint(request)
    assert second != first
    assert request.model_dump(mode='json', by_alias=True) == original
