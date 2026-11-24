"""Latest view is an explicit, reversible projection of the fixed wire contract."""
from copy import deepcopy
import html as html_module
import json
import re

import pytest

from adapters.gut_barrier.frontend_template import expand_dynamic_report, load_static_template
from adapters.gut_barrier.latest_view import arrange_sections, display_block
from adapters.gut_barrier.rendering import render_report_html
from adapters.gut_barrier.render_context import make_render_context, resolve_render_context
from modules.gut_barrier.display_versions import (
    PRE_XU_DISPLAY_TEMPLATE as LATEST, LEGACY_DISPLAY_TEMPLATE as LEGACY,
    LATEST_DISPLAY_TEMPLATE as CURRENT,
)
from modules.gut_barrier.pipeline import GutBarrierPipeline
from modules.gut_barrier.publication import project_callback_report
from modules.gut_barrier.schemas import GutBarrierRequest
from scripts.render_gut_barrier_callback import render_callback
from tests.unit.gut_barrier.final_input_fixture import final_input
from tests.unit.gut_barrier.test_dynamic_publication import clinical_report
from tests.unit.gut_barrier.preview.support import Parsed, visible_source


def section(report, name):
    return next(s for s in report['sections'] if s['id'] == name)


def block(report, name):
    return next(b for s in report['sections'] for b in s['blocks'] if b.get('title') == name)


def public():
    return project_callback_report(clinical_report(pattern='---', crp='>3.0'))


def embedded(html):
    return json.loads(re.search(r'<script type="application/json" id="report-data">(.*?)</script>', html, re.S)[1])


def test_bare_receipts_keep_old_asset_even_with_untrusted_metadata(historical_templates):
    report=public()
    before=deepcopy(report)
    old=render_report_html(report)
    assert old == render_report_html(report, template_version=LEGACY)
    assert '3.6 第五原则' in visible_source(old)
    report['meta']['displayTemplateVersion']=LATEST
    assert '3.6 第五原则' in visible_source(render_report_html(report))
    report['meta'].pop('displayTemplateVersion')
    assert report == before


def test_explicit_latest_keeps_wire_columns_keys_and_values(historical_templates):
    report=public(); before=deepcopy(report)
    html=render_report_html(report, template_version=LATEST); body=visible_source(html)
    assert embedded(html)==before==report
    assert set(report)=={'schemaVersion','meta','sections'}
    assert set(report['meta'])=={'title','reportId'}
    assert [s['id'] for s in report['sections']]==['overview','disease_risks','appendix']
    assert block(report,'三项检测指标')['columns']==['检测指标','检测结果','参考范围','结果评价','单项评分']
    for table in section(report,'disease_risks')['blocks']:
        if table['kind']=='table': assert table['columns']==['疾病','风险指数','风险等级','机制关联']
    for heading in ['1.1 综合评估指数','1.2 检测项目与结果总览','3.1 修复总原则','3.5 生活方式干预','5.3 免责声明','5.4 参考文献']:
        assert heading in body
    for heading in ['1.3 肠道屏障功能评分','1.4 肠道屏障受损类型','1.5 慢性低度炎症评估','3.6 第五原则','5.5 参考文献']:
        assert heading not in body
    assert 'data-label="检测项目"' in body and 'data-label="反映维度"' in body
    assert 'data-label="单项评分"' not in body and 'data-label="风险指数"' not in body


def test_zero_unknown_and_non_numeric_values_are_not_invented_or_lost(historical_templates):
    report=public(); rows=block(report,'三项检测指标')['rows']
    rows[0][1]=None
    cards=block(report,'肠道屏障功能评分（IBFC）')['items']; cards[0]['value']=0
    html=render_report_html(report,template_version=LATEST);body=visible_source(html)
    assert 'IBFC评分 0' in body and '0 / 15' in body
    assert '未提供' in body and '68%' not in body
    assert embedded(html)==report
    cards[0]['value']=None
    body=visible_source(render_report_html(report,template_version=LATEST))
    assert 'IBFC评分 0' not in body


def test_latest_facts_keep_sample_type_and_future_fields_without_showing_report_id(historical_templates):
    report=public()
    facts=block(report,'受检者信息')['items']
    next(x for x in facts if x['label']=='样本类型')['value']='SAMPLE-TYPE-SENTINEL'
    facts.extend([{'label':'新增资料','value':'UNKNOWN-SENTINEL'}, {'label':'确认值','value':False}, {'label':'报告编号','value':'HIDDEN-ID'}])
    body=visible_source(render_report_html(report,template_version=LATEST))
    assert 'SAMPLE-TYPE-SENTINEL' in body and 'UNKNOWN-SENTINEL' in body and 'False' in body
    assert 'HIDDEN-ID' not in body


def test_safety_blocks_survive_compact_overview(historical_templates):
    report=project_callback_report(clinical_report(context={'allergies':'鱼类过敏','liverAndKidneyFunction':'肾功能不全','immuneStatus':'免疫抑制','acuteInfection':True}))
    body=visible_source(render_report_html(report,template_version=LATEST))
    for text in ['鱼类/海鲜过敏','肝肾功能','免疫状态','急性感染']: assert text in body


def test_new_reference_numbering_retains_unique_anchors_and_all_targets(historical_templates):
    body=visible_source(render_report_html(public(),template_version=LATEST))
    ids=[attrs['id'] for _,attrs in Parsed(body).tags if 'id' in attrs]
    assert len(ids)==len(set(ids))
    targets=set(re.findall(r'href="#(reference-[^"]+)"',body))
    assert targets and targets<=set(ids)
    assert 'reference-primary-1' in ids and 'reference-supplement-1-1' in ids
    for number in [1,2,3,5,6,7,8,9]:assert f'figure-{number}' in ids
    assert body.index('id="figure-1"')<body.index('id="figure-2"')


@pytest.mark.parametrize('version',['gut-barrier-static.v99','../../secret',[],False,''])
def test_unknown_display_version_fails_closed(version):
    with pytest.raises(ValueError,match='unsupported'):
        render_report_html(public(),template_version=version)


def test_new_expansion_is_idempotent_without_overwriting_legacy_asset(historical_templates):
    report=public();old=load_static_template()
    expanded=expand_dynamic_report(report,template_version=LATEST)
    assert expand_dynamic_report(expanded,template_version=LATEST)==expanded
    assert load_static_template()==old
    assert '5.3 风险指数标尺说明' in json.dumps(old,ensure_ascii=False)


@pytest.mark.asyncio
async def test_current_pipeline_binding_is_internal_and_summary_is_text():
    data=final_input();data['fileList']=[]
    internal,audit=await GutBarrierPipeline().generate(GutBarrierRequest.model_validate(data),report_id='gbr_new_test',evaluation_date='2026-09-25')
    report=project_callback_report(internal);before=deepcopy(report)
    assert audit['displayTemplateVersion']==internal['meta']['displayTemplateVersion']==CURRENT
    assert 'displayTemplateVersion' not in json.dumps(report)
    html=render_report_html(report,template_version=CURRENT);body=visible_source(html)
    assert 'class="focus-cards"' not in body
    assert '本章小结' in body
    assert '3.6 第五原则' not in body
    assert '3.5 生活方式干预' in visible_source(render_report_html(internal))
    assert embedded(html)==before==report
    disease=section(report,'disease_risks')
    count=sum(b['kind']=='table' and b['columns']==['疾病','风险指数','风险等级','机制关联']
              for b in disease['blocks'])
    summary_title=f'2.{count+1} 本章小结'
    summaries=[b for b in disease['blocks'] if b.get('title')==summary_title]
    assert len(summaries)==1 and summaries[0]['kind']=='paragraph'
    assert set(summaries[0])=={'kind','title','text'}
    summary_prose=summaries[0]['text']
    paragraphs=summary_prose.split('\n\n')
    assert len(paragraphs)==2 and all(paragraphs)
    summary_anchor='disease_risks-'+summary_title.split()[0].replace('.', '-')
    summary_body=re.search(r'<div class="block" id="'+re.escape(summary_anchor)+r'">(.*?)</div>', body, re.S)[1]
    rendered_paragraphs=re.findall(r'<p>(.*?)</p>', summary_body, re.S)
    assert len(rendered_paragraphs)==2
    assert [html_module.unescape(paragraph) for paragraph in rendered_paragraphs]==paragraphs
    assert body.count(f'<h3>{summary_title}</h3>')==1
    assert all(term in summary_prose for term in ('屏障', '内毒素', '血液循环', '慢性低度炎症', '菌群稳态'))
    assert summary_prose.endswith('修复肠道屏障功能，恢复肠道菌群稳态。')
    assert summary_prose.count('\n\n')==1 and all('\n' not in paragraph for paragraph in paragraphs)
    assert '复核' not in summary_prose and '试行规则评分结果' not in summary_prose and '本次试行规则下' not in summary_prose
    assert not any(b.get('title') in {'本次相关因素与复核依据', '影像检查待核实事项'} for b in disease['blocks'])
    assert not any(b['kind']=='list' and '本章小结' in (b.get('title') or '') for b in disease['blocks'])
    targets=set(re.findall(r'href="#(reference-[^"]+)"',body));ids=set(re.findall(r'id="([^"]+)"',body))
    assert targets<=ids


def test_registered_zero_stays_visible_but_not_in_attention_chart_or_empty_cards(historical_templates):
    report=public();disease=section(report,'disease_risks')
    disease['blocks']=[{'kind':'callout','title':'本次关注指数','text':'健康关注指数','tone':'default'},
        {'kind':'table','title':'2.1 消化系统疾病风险评估','columns':['疾病','风险指数','风险等级','机制关联'],
         'rows':[['注册模型A',0,'低风险','本项数值来自独立的已配置模型，不与本章自定义关注指数比较。']]},
        {'kind':'list','title':'本章小结','items':[]}]
    body=visible_source(render_report_html(report,template_version=LATEST))
    assert '独立模型指数：0' in body and '注册模型A' in body
    assert 'risk-fill' not in body and 'id="figure-4"' not in body and '本章小结' not in body


def test_same_three_column_view_does_not_guess_ambiguous_focus_matches():
    sections=[{'id':'disease_risks','blocks':[{'kind':'table','columns':list(('疾病','风险指数','风险等级','机制关联')),
              'title':'2.1 消化系统疾病风险评估','rows':[['A',3,'基础关注','x'],['A',4,'加强关注','y']]}]}]
    source={'kind':'list','title':'本章小结','items':['A：建议']}
    assert display_block('disease_risks',source,sections)==source


def test_sidecar_matches_report_and_is_not_a_callback_change(tmp_path, historical_templates):
    report=public();context=make_render_context(report,LATEST)
    callback={'type':'gutBarrier','schemaVersion':'2.0','status':'partial','report':report}
    before=deepcopy(callback)
    target=render_callback(callback,tmp_path/'rendered',render_context=context)
    assert '3.5 生活方式干预' in visible_source(target.read_text(encoding="utf-8"))
    assert resolve_render_context(report,context)==LATEST
    assert callback==before
    assert json.loads((target.parent/'render-context.json').read_text(encoding="utf-8"))==context
    with pytest.raises(FileExistsError):render_callback(callback,target.parent,render_context=context)
    wrong=deepcopy(report);wrong['meta']['reportId']='another'
    with pytest.raises(ValueError,match='does not match'):resolve_render_context(wrong,context)
    changed=deepcopy(context);changed['templateVersion']=None
    with pytest.raises(ValueError):resolve_render_context(report,changed)
    with pytest.raises(ValueError,match='disagree'):render_callback(callback,tmp_path/'wrong',template_version=LEGACY,render_context=context)


def test_partial_document_does_not_hide_data_without_cover_or_graphs(historical_templates):
    report=public()
    report['sections']=[section(report,'overview')]
    block(report,'受检者信息')['items'].append({'label':'姓名','value':'PARTIAL-PATIENT'})
    block(report,'肠道屏障功能评分（IBFC）')['items'][0]['detail']='PARTIAL-SCORE-DETAIL'
    body=visible_source(render_report_html(report,template_version=LATEST))
    assert 'PARTIAL-PATIENT' in body and 'PARTIAL-SCORE-DETAIL' in body


@pytest.mark.parametrize('change',['hidden_appendix','extra_section'])
def test_unknown_layout_keeps_score_columns_and_facts_visible(change, historical_templates):
    report=public()
    if change=='hidden_appendix':
        section(report,'appendix')['visible']=False
    else:
        report['sections'].append({'id':'additional','title':'附加信息','subtitle':None,'visible':True,
                                  'blocks':[{'kind':'paragraph','title':'附加内容','text':'MORE-DATA'}]})
    block(report,'受检者信息')['items'].append({'label':'姓名','value':'PATIENT-SENTINEL'})
    section(report,'disease_risks')['blocks']=[{'kind':'table','title':'2.1 消化系统疾病风险评估',
        'columns':['疾病','风险指数','风险等级','机制关联'],
        'rows':[['关注方向',7,'优先评估','检测依据']]}]
    body=visible_source(render_report_html(report,template_version=LATEST))
    assert 'PATIENT-SENTINEL' in body and 'data-label="风险指数">7<' in body
    assert 'IBFC评分' in body


def test_nonredundant_inflammation_and_extra_card_data_survive_latest_view(historical_templates):
    report=public();overview=section(report,'overview')
    overview['blocks']=[b for b in overview['blocks'] if b.get('title') not in
                        {'1.5 慢性低度炎症评估','1.4 肠道屏障受损类型与受损状况'}]
    summary=block(report,'1.1 检测综合评估结论')
    next(r for r in summary['rows'] if r[0]=='炎症状态')[1]='INFLAMMATION-SENTINEL'
    cards=block(report,'肠道屏障功能评分（IBFC）')['items']
    cards[0]['detail']='SCORE-DETAIL'
    cards.append({'label':'新增评估','value':'FUTURE-CARD','detail':None})
    body=visible_source(render_report_html(report,template_version=LATEST))
    for value in ['INFLAMMATION-SENTINEL','SCORE-DETAIL','FUTURE-CARD','三项未见异常']:
        assert value in body


def test_latest_view_escapes_hostile_source_and_long_disease_names(historical_templates):
    report=public()
    long_name='很长的疾病鉴别名称'*40+'<img src=x onerror=alert(1)>'
    disease=section(report,'disease_risks')
    disease['blocks']=[{'kind':'table','title':'2.1 消化系统疾病风险评估',
        'columns':['疾病','风险指数','风险等级','机制关联'],
        'rows':[[long_name,3,'基础关注','<script>alert(1)</script>']]}]
    html=render_report_html(report,template_version=LATEST)
    assert embedded(html)==report
    body=visible_source(html)
    assert '<img src=x' not in body and '<script>alert' not in body
    assert '很长的疾病鉴别名称'*40 in body and '&lt;img' in body
