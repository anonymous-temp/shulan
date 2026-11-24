"""Temporary, dependency-free factor trial core; no application imports or stores.

Validation checks structure and provenance, not clinical correctness. Medical
interpretation must still be reviewed. Real patient artifacts stay outside Git.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from datetime import datetime
from datetime import timezone
from pathlib import Path

VERSION = 'questionnaire-factor-trial-v2'
RELATIONS = {'risk_factor', 'candidate_contributor', 'possible_source', 'coexisting_finding'}
PRESENCE = {'present', 'absent', 'unknown', 'conflicting'}
JUDGMENTS = {'supported', 'needs_verification', 'not_supported', 'conflicting'}
RELATION_LABELS = {
    'risk_factor': '风险因素',
    'candidate_contributor': '可能参与的因素',
    'possible_source': '可能来源',
    'coexisting_finding': '并存发现',
}
STATE_LABELS = {
    'present': '已报告存在',
    'absent': '明确否认',
    'unknown': '未知',
    'conflicting': '存在冲突',
    'supported': '支持相应关系',
    'needs_verification': '待核实',
    'not_supported': '当前不支持',
}


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def title_key(value):
    text = re.sub(r'^\s*\d+[、.．]\s*', '', str(value))
    return ''.join(c for c in unicodedata.normalize('NFKC', text) if not c.isspace() and c not in '，,。.?？!！')


def build_case(report_path, questionnaire_path, workbook_path, *, sheet=None, answer_row=2):
    # Optional dependency is imported only for the workbook input adapter.
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter

    report, questionnaire = read_json(report_path), read_json(questionnaire_path)
    evidence = []
    historical_interpretations = []

    def walk(node, pointer='', context=None):
        if isinstance(node, dict):
            context = dict(context or {})
            for key in ('reportTime', 'observedAt', 'age', 'gender', 'sex'):
                if node.get(key) is not None:
                    context[key] = {'value': node[key], 'pointer': pointer + '/' + key}
            subject = node.get('subject')
            if isinstance(subject, dict):
                for key in ('age', 'gender', 'sex'):
                    if subject.get(key) is not None:
                        context[key] = {'value': subject[key], 'pointer': pointer + '/subject/' + key}
            if 'itemName' in node and 'resultValue' in node:
                evidence.append(
                    {'id': 'report:' + pointer, 'kind': 'report_extraction', 'raw': node, 'context': context}
                )
                return
            for key, value in node.items():
                if key == 'aiSummary' and value:
                    historical_interpretations.append(
                        {
                            'id': 'interpretation:report:' + pointer + '/aiSummary',
                            'kind': 'historical_ai_interpretation',
                            'sourceArtifact': 'report',
                            'pointer': pointer + '/aiSummary',
                            'text': value if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
                        }
                    )
                if key in {'aiSummary', 'processing', 'unresolvedItems'}:
                    continue
                escaped = key.replace('~', '~0').replace('/', '~1')
                child = pointer + '/' + escaped
                if key == 'expansion' and '/sections/' in pointer:
                    evidence.append(
                        {'id': 'report:' + child, 'kind': 'report_extraction', 'raw': value, 'context': context}
                    )
                elif key in {'age', 'gender', 'sex'} and isinstance(value, (str, int, float)):
                    evidence.append(
                        {'id': 'report:' + child, 'kind': 'reported_demographic', 'raw': value, 'context': context}
                    )
                elif isinstance(value, (dict, list)):
                    walk(value, child, context)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, pointer + '/' + str(index), context)

    walk(report)
    for page_index, question_page in enumerate(questionnaire.get('pages', [])):
        for question_index, question in enumerate(question_page.get('elements', [])):
            description = question.get('description')
            if isinstance(description, str) and description.strip():
                pointer = f'/pages/{page_index}/elements/{question_index}/description'
                historical_interpretations.append(
                    {
                        'id': 'interpretation:questionnaire:' + pointer,
                        'kind': 'historical_ai_interpretation',
                        'sourceArtifact': 'questionnaire',
                        'pointer': pointer,
                        'questionName': question.get('name'),
                        'text': description,
                    }
                )
    questions = [e for p in questionnaire.get('pages', []) for e in p.get('elements', [])]
    titles = {}
    for question in questions:
        titles.setdefault(title_key(question.get('title', '')), []).append(question.get('name'))
    workbook = load_workbook(workbook_path, read_only=True, data_only=False)
    try:
        page = workbook[sheet] if sheet else workbook.active
        if not 2 <= answer_row <= page.max_row:
            raise ValueError('answer row must identify one existing respondent')
        answers = []
        submitted_at = None
        for column in range(1, page.max_column + 1):
            header = page.cell(1, column)
            cell = page.cell(answer_row, column)
            title = str(header.value or '')
            if '提交' in title and '时间' in title:
                submitted_at = str(cell.value) if cell.value is not None else None
            if not re.match(r'^\s*\d+[、.．]', title):
                continue
            if cell.data_type == 'f' or header.data_type == 'f':
                raise ValueError('formula cells cannot serve as raw questionnaire evidence')
            if not isinstance(cell.value, (str, int, float, bool, type(None))):
                raise ValueError('unsupported answer cell type')
            matches = titles.get(title_key(title), [])
            answer = {
                'id': f'answer:{page.title}!{get_column_letter(column)}{answer_row}',
                'kind': 'user_answer',
                'questionRef': f'question:{page.title}!{get_column_letter(column)}1',
                'question': title,
                'raw': cell.value,
                'answerState': 'unknown' if cell.value is None or str(cell.value).strip() == '' else 'provided',
                'alignment': 'title_only' if len(matches) == 1 else 'ambiguous' if matches else 'unmatched',
                'questionNameHint': matches[0] if len(matches) == 1 else None,
            }
            answers.append(answer)
        if not answers:
            raise ValueError('no numbered question columns found in workbook')
    finally:
        workbook.close()
    evidence.extend(answers)
    source_files = {
        name: {'sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest()}
        for name, path in [('report', report_path), ('questionnaire', questionnaire_path), ('answers', workbook_path)]
    }
    case = {
        'version': VERSION,
        'sourceFiles': source_files,
        'reportTime': report.get('reportTime'),
        'answeredAt': submitted_at,
        'answerRow': answer_row,
        'questionnaireQuestionCount': len(questions),
        'answerCount': len(answers),
        'evidence': evidence,
        'historicalInterpretations': historical_interpretations,
        'reportSourceAvailability': {
            'sourceArtifactKind': 'report_interpretation_json',
            'originalDocumentProvided': False,
            'originalDocumentVerified': False,
            'reportAiSummaryAvailable': any(i['sourceArtifact'] == 'report' for i in historical_interpretations),
            'note': '只提供了解读 JSON，未提供原始体检 PDF 或图像；原文件原文尚不能逐字核验。',
        },
        'limitations': [
            '题干匹配仅供定位，不证明属于同一发布版本；不按题号猜测映射。',
            '报告来自解读 JSON，本工具没有复核原始体检文件；报告 AI 摘要和问卷承接不作为患者事实。',
            '当前习惯与历史检查分别保留时间，不能默认暴露发生在体检之前。',
        ],
    }
    case['fingerprint'] = digest(case)
    return case


def require(condition, message):
    if not condition:
        raise ValueError(message)


def output_directory(value):
    destination = Path(value).resolve()
    repository = Path(__file__).resolve().parents[4]
    approved = (repository / 'api_test/out/record-factor-trial').resolve()
    require(
        not destination.is_relative_to(repository) or destination.is_relative_to(approved),
        'repository output is limited to the ignored api_test/out/record-factor-trial directory',
    )
    return destination


def validate_analysis(case, analysis, catalog, knowledge):
    """Fail closed on invalid references; never silently repair medical content."""
    require(analysis.get('inputFingerprint') == case['fingerprint'], 'analysis belongs to different input')
    concepts = {c['id']: c for c in catalog['concepts']}
    sources = {s['id']: s for s in knowledge['sources']}
    facts = {e['id']: e for e in case['evidence']}
    require(len(sources) == len(knowledge['sources']), 'duplicate knowledge source ID')
    risks = analysis.get('risks', [])
    require(isinstance(risks, list) and risks, 'risks must be a nonempty list')
    risk_ids = {r['id'] for r in risks}
    require(len(risk_ids) == len(risks), 'duplicate risk ID')

    def check_refs(refs, *, nonempty=False):
        require(isinstance(refs, list) and (not nonempty or bool(refs)), 'invalid evidence references')
        require(all(isinstance(ref, str) and ref in facts for ref in refs), 'unknown personal evidence reference')

    for risk in risks:
        require(set(risk) == {'id', 'label', 'interpretation', 'evidenceRefs'}, 'unexpected risk fields')
        check_refs(risk.get('evidenceRefs'), nonempty=True)
        require(isinstance(risk['label'], str) and risk['label'].strip(), 'missing risk label')
        require(
            isinstance(risk.get('interpretation'), str) and risk['interpretation'].strip(), 'missing risk explanation'
        )
    factors = analysis.get('factors', [])
    require(isinstance(factors, list) and factors, 'factors must be a nonempty list')
    seen = set()
    for factor in factors:
        require(
            set(factor)
            == {
                'conceptId',
                'targetRiskId',
                'relationType',
                'presence',
                'judgment',
                'temporalMatch',
                'evidenceRefs',
                'counterEvidenceRefs',
                'knowledgeRefs',
                'unverifiedConditions',
                'interpretation',
            },
            'unexpected factor fields',
        )
        identity = (factor.get('conceptId'), factor.get('targetRiskId'))
        require(identity not in seen, 'duplicate concept for target risk')
        seen.add(identity)
        require(factor.get('conceptId') in concepts, 'unknown controlled factor concept')
        concept = concepts[factor['conceptId']]
        relation = factor.get('relationType')
        require(relation in RELATIONS and relation in concept['allowedRelations'], 'unsupported relation type')
        require(factor.get('targetRiskId') in risk_ids, 'unknown target risk')
        require(factor.get('presence') in PRESENCE, 'invalid personal presence')
        require(factor.get('judgment') in JUDGMENTS, 'invalid relationship judgment')
        require(factor.get('temporalMatch') in {'supported', 'unknown', 'not_applicable'}, 'invalid temporal status')
        check_refs(factor.get('evidenceRefs'), nonempty=True)
        check_refs(factor.get('counterEvidenceRefs', []))
        require(isinstance(factor.get('unverifiedConditions'), list), 'missing unverified conditions')
        require(all(isinstance(c, str) and c.strip() for c in factor['unverifiedConditions']), 'invalid condition')
        require(
            isinstance(factor.get('interpretation'), str) and factor['interpretation'].strip(), 'missing interpretation'
        )
        if factor['presence'] in {'unknown', 'conflicting', 'absent'}:
            require(factor['judgment'] != 'supported', 'unknown, absent or conflicting factor cannot be supported')
        for ref in factor['evidenceRefs']:
            if facts[ref].get('answerState') == 'unknown':
                require(
                    factor['presence'] not in {'present', 'absent'}, 'empty answer cannot establish presence or denial'
                )
        citations = factor.get('knowledgeRefs', [])
        require(isinstance(citations, list) and citations, 'factor needs medical source references')
        for citation in citations:
            require(isinstance(citation, dict), 'invalid medical citation')
            source = sources.get(citation.get('sourceId'))
            require(source is not None, 'unknown medical source')
            quote = citation.get('quote', '')
            require(isinstance(quote, str) and bool(quote.strip()), 'missing supporting source excerpt')
            require(quote in source.get('sourceText', ''), 'citation is not in source text')
            require(
                source.get('provenance') in {'public_source_excerpt', 'evimed_original_text'},
                'summary is not source text',
            )
    if 'review' in analysis:
        from tests.unit._support.harnesses.questionnaire_factor_review import validate_review

        validate_review(case, analysis, catalog, knowledge)
    return analysis


def build_views(analysis, catalog):
    concepts = {c['id']: c for c in catalog['concepts']}
    result = []
    for factor in analysis['factors']:
        concept = concepts[factor['conceptId']]
        relation = factor['relationType']
        result.append(
            {
                'conceptId': factor['conceptId'],
                'targetRiskId': factor['targetRiskId'],
                'clinical': {
                    'label': concept['label'],
                    'relation': RELATION_LABELS[relation],
                    'text': factor['interpretation'],
                },
                'mesh': {
                    'relationHeading': 'Risk Factors' if relation == 'risk_factor' else None,
                    'relationDescriptorId': 'D012307' if relation == 'risk_factor' else None,
                    'mappingStatus': 'relation_category_only',
                    'note': '用于关系分类和检索；不是个人病因证明，不代表具体因素概念已完成 MeSH 映射。',
                },
                'snomedInspired': {
                    'code': None,
                    'mappingStatus': 'not_coded',
                    'directCauseAsserted': False,
                    'relation': relation,
                    'personalPresence': factor['presence'],
                    'note': '项目关系语义示例，不是已验证的 SNOMED CT 编码或后组配表达式。',
                },
            }
        )
    return result


def render_markdown(result):
    case, analysis = result['case'], result['analysis']
    facts = {e['id']: e for e in case['evidence']}
    lines = [
        '# 问卷案例：因素分析与表达对照',
        '',
        f'运行方式：`{result["run"]["mode"]}`；生产 API 和数据库未调用。',
        '',
        '本工具验证输入、引用和输出结构，不证明医学推理正确；结果尚需医学人员审核。',
        '',
        f'体检时间：{case["reportTime"]}；回答时间：{case["answeredAt"]}。',
        '',
        f'JSON 问卷 {case["questionnaireQuestionCount"]} 题；Excel 回答 {case["answerCount"]} 项。',
        '',
        '## 风险方向',
        '',
    ]
    for risk in analysis['risks']:
        lines.extend([f'- **{risk["label"]}**：{risk["interpretation"]}'])
    lines.extend(['', '## 因素分析', ''])
    for factor, view in zip(analysis['factors'], result['views'], strict=True):
        lines.extend(
            [
                f'### {view["clinical"]["label"]}',
                '',
                f'关系：{view["clinical"]["relation"]}；个人状态：{STATE_LABELS[factor["presence"]]}；'
                f'判断：{STATE_LABELS[factor["judgment"]]}。',
                '',
                factor['interpretation'],
                '',
                '个人证据：',
                '',
            ]
        )
        for ref in factor['evidenceRefs']:
            evidence = facts[ref]
            raw = (
                json.dumps(evidence['raw'], ensure_ascii=False)
                if isinstance(evidence['raw'], dict)
                else str(evidence['raw'])
            )
            lines.append(f'- `{ref}`：{evidence.get("question", "")} {raw}')
        if factor['counterEvidenceRefs']:
            lines.extend(['', '反向或冲突证据：' + '、'.join(factor['counterEvidenceRefs'])])
        if factor['unverifiedConditions']:
            lines.extend(['', '尚待核实：' + '；'.join(factor['unverifiedConditions']) + '。'])
        lines.extend(['', '医学来源：' + '、'.join(c['sourceId'] for c in factor['knowledgeRefs']), ''])
    lines.extend(
        ['## 三种表达方式对照', '', '| 因素 | 临床关系 | MeSH 分类 | 关系语义示例 |', '| --- | --- | --- | --- |']
    )
    for view in result['views']:
        lines.append(
            f'| {view["clinical"]["label"]} | {view["clinical"]["relation"]} | '
            f'{view["mesh"]["relationHeading"] or "未映射，不强行归为诱发因素"} | '
            f'{view["snomedInspired"]["relation"]}；未作直接因果断言 |'
        )
    lines.extend(
        ['', '三种视图来自同一结果；MeSH 不执行诊断，关系示例未完成 SNOMED CT 编码。', '', '## 数据与判断边界', '']
    )
    lines.extend('- ' + str(item) for item in [*case['limitations'], *analysis.get('limitations', [])])
    lines.extend(['', '## 医学参考资料', ''])
    for source in result['knowledge']['sources']:
        lines.append(f'- {source["id"]} [{source["title"]}]({source["url"]})；来源方式：{source["provenance"]}。')
    return '\n'.join(lines) + '\n'


def render_html(result):
    def esc(value):
        return html.escape(str(value), quote=True)

    case, analysis = result['case'], result['analysis']
    pieces = [
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<title>问卷因素分析对照</title><style>body{max-width:1080px;margin:32px auto;padding:0 22px;'
        'color:#20303a;background:#fff;font:16px/1.75 system-ui}h1{font-size:27px}h2{margin-top:32px}'
        'article{border:1px solid #dce5e7;border-radius:10px;padding:18px 22px;margin:14px 0}'
        '.meta{color:#52626b;font-size:14px}.tag{color:#16665b;background:#eaf5f1;padding:4px 9px;'
        'border-radius:5px}table{border-collapse:collapse;width:100%}td,th{padding:10px;text-align:left;'
        'border-bottom:1px solid #ddd;vertical-align:top}code{overflow-wrap:anywhere}li{margin:6px 0}'
        'details{margin-top:10px}pre{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#16665b}'
        '</style><body><h1>问卷因素分析对照</h1>',
        '<p class="meta">' + esc(result['run']['mode']) + ' · 医学审核待完成 · 未调用生产 API 或数据库</p>',
        '<p>结构校验通过表示来源定位与数据格式符合约束，不代表已证明个人病因。</p>',
        f'<p class=meta>体检：{esc(case["reportTime"])}；回答：{esc(case["answeredAt"])}。'
        f'问卷定义 {case["questionnaireQuestionCount"]} 题，实际回答 {case["answerCount"]} 项。</p>',
        '<h2>需要关注的健康风险</h2>',
    ]
    for risk in analysis['risks']:
        pieces.append(
            '<article><strong>' + esc(risk['label']) + '</strong><p>' + esc(risk['interpretation']) + '</p></article>'
        )
    facts = {e['id']: e for e in case['evidence']}
    sources = {s['id']: s for s in result['knowledge']['sources']}
    pieces.append('<h2>具体因素与支持依据</h2>')
    for factor, view in zip(analysis['factors'], result['views'], strict=True):
        pieces.append(
            '<article><strong>'
            + esc(view['clinical']['label'])
            + '</strong> <span class="tag">'
            + esc(STATE_LABELS[factor['judgment']])
            + '</span><p>'
            + esc(factor['interpretation'])
            + '</p>'
        )
        pieces.append(
            '<p class=meta>关系：'
            + esc(view['clinical']['relation'])
            + '；个人状态：'
            + esc(STATE_LABELS[factor['presence']])
            + '；体检时段匹配：'
            + esc(factor['temporalMatch'])
            + '</p>'
        )
        if factor['unverifiedConditions']:
            pieces.append('<p>待核实：' + esc('；'.join(factor['unverifiedConditions'])) + '</p>')
        pieces.append('<details><summary>查看个人证据与医学来源</summary><ul>')
        for ref in factor['evidenceRefs'] + factor['counterEvidenceRefs']:
            item = facts[ref]
            raw = json.dumps(item['raw'], ensure_ascii=False) if isinstance(item['raw'], dict) else str(item['raw'])
            pieces.append('<li><code>' + esc(ref) + '</code>：' + esc(item.get('question', '') + ' ' + raw) + '</li>')
        for citation in factor['knowledgeRefs']:
            source = sources[citation['sourceId']]
            # Display URLs as text so an untrusted provider cannot inject a scheme.
            pieces.append('<li>' + esc(source['title']) + '：' + esc(source['url']) + '</li>')
        pieces.append('</ul></details></article>')
    pieces.append(
        '<h2>同一结论的三种表达</h2><table><tr><th>因素</th><th>临床表达</th>'
        '<th>MeSH 分类</th><th>关系语义示例</th></tr>'
    )
    for view in result['views']:
        pieces.append(
            '<tr><td>'
            + esc(view['clinical']['label'])
            + '</td><td>'
            + esc(view['clinical']['relation'])
            + '</td><td>'
            + esc(view['mesh']['relationHeading'] or '未映射')
            + '</td><td>'
            + esc(view['snomedInspired']['relation'])
            + '</td></tr>'
        )
    pieces.append(
        '</table><p class=meta>MeSH 用于关系分类及检索；关系语义示例尚未完成 SNOMED CT 编码。'
        '三种视图不构成三套独立诊断。</p><h2>数据与判断边界</h2><ul>'
    )
    pieces.extend('<li>' + esc(value) + '</li>' for value in case['limitations'] + analysis.get('limitations', []))
    pieces.append('</ul></body></html>')
    return ''.join(pieces)


def write_outputs(output_dir, case, analysis, catalog, knowledge, *, mode, telemetry=None):
    destination = output_directory(output_dir)
    validate_analysis(case, analysis, catalog, knowledge)
    result = {
        'version': VERSION,
        'catalog': catalog,
        'catalogFingerprint': digest(catalog),
        'coreFingerprint': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'run': {
            'mode': mode,
            'createdAt': datetime.now(timezone.utc).isoformat(),
            'medicalReview': 'pending',
            'telemetry': telemetry or {},
        },
        'case': case,
        'analysis': analysis,
        'knowledge': knowledge,
        'views': build_views(analysis, catalog),
    }
    if 'review' in analysis:
        from tests.unit._support.harnesses import questionnaire_factor_review as review

        result['reviewRendererFingerprint'] = hashlib.sha256(Path(review.__file__).read_bytes()).hexdigest()
        result['reviewProjection'] = review.build_review_projection(result)
        markdown = review.render_review_markdown(result)
        html_content = review.render_review_html(result)
    else:
        markdown = render_markdown(result)
        html_content = render_html(result)
    destination.mkdir(parents=True, exist_ok=True)
    files = {
        'result.json': json.dumps(result, ensure_ascii=False, indent=4) + '\n',
        'comparison.md': markdown,
        'comparison.html': html_content,
    }
    for filename, content in files.items():
        path = destination / filename
        require(not path.exists(), f'output already exists: {filename}')
    for filename, content in files.items():
        (destination / filename).write_text(content, encoding='utf-8')
    return result
