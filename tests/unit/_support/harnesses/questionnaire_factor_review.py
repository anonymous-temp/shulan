"""Review-only evidence projection for the temporary factor trial.

This module never reconstructs a historical execution trace. It validates and
renders a retrospective explanation, without application imports or I/O.
"""

from __future__ import annotations

import html
import re
from copy import deepcopy
from urllib.parse import urlsplit

VERSION = 'factor-evidence-review-v2'
STATES = {'present', 'absent', 'unknown', 'conflicting'}
REPORT_KINDS = {'report_extraction', 'reported_demographic'}
FACT_KINDS = REPORT_KINDS | {'user_answer'}
STATE_LABELS = {'present': '存在', 'absent': '明确否认', 'unknown': '未知', 'conflicting': '冲突'}
EFFECT_LABELS = {'supports': '增加支持', 'weakens': '削弱支持', 'conflicts': '出现冲突', 'no_change': '状态未变'}
JUDGMENT_LABELS = {
    'supported': '支持相应风险关系',
    'needs_verification': '仍待核实',
    'not_supported': '当前不支持',
    'conflicting': '资料存在冲突',
}
ALIGNMENT_LABELS = {'title_only': '仅题干对应', 'ambiguous': '存在多个可能对应题目', 'unmatched': '未匹配问卷定义'}
TEMPORAL_LABELS = {'supported': '时段相符', 'unknown': '时段尚未核实', 'not_applicable': '无需时段匹配'}
PROVENANCE_LABELS = {'public_source_excerpt': '公开资料原文摘录', 'evimed_original_text': 'EviMed 检索原文'}
MODE_LABELS = {
    'assistant_analysis_replay': '助手整理、程序校验回放；未运行项目模型或私有知识库',
    'live_aion_evimed': '真实调用 AION 模型与 EviMed 知识检索服务',
    'synthetic': '合成资料离线验证；未进行真实医学服务调用',
}
FIELD_LABELS = {
    'itemName': '项目',
    'resultValue': '结果',
    'unit': '单位',
    'referenceRange': '参考范围',
    'flag': '报告标记',
    'observedAt': '记录时间',
    'reportTime': '报告时间',
    'age': '年龄',
    'gender': '性别',
    'sex': '性别',
    'findings': '检查所见',
    'diagnosis': '解读 JSON 中的诊断记录',
    'description': '记录描述',
    'year': '年份',
    'publicationDate': '发表日期',
    'publisher': '发布机构',
    'journal': '期刊',
    'authors': '作者',
    'knowledgeBaseRevision': '知识库版本',
    'studyType': '研究类型',
}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _text(value, field):
    _require(isinstance(value, str) and bool(value.strip()), f'{field} must be nonempty text')


def _shape(value, keys, name):
    _require(isinstance(value, dict) and set(value) == set(keys.split()), f'invalid {name} fields')


def _index(rows, name, key='id'):
    _require(isinstance(rows, list), f'{name} must be a list')
    result = {}
    for row in rows:
        _require(isinstance(row, dict), f'invalid {name} entry')
        identifier = row.get(key)
        _text(identifier, f'{name} identifier')
        _require(identifier not in result, f'duplicate {name} identifier')
        result[identifier] = row
    return result


def _refs(value, index, name, *, nonempty=False, kinds=None):
    _require(isinstance(value, list) and (not nonempty or bool(value)), f'invalid {name} references')
    _require(all(isinstance(ref, str) for ref in value), f'invalid {name} reference type')
    _require(len(set(value)) == len(value), f'duplicate {name} reference')
    for ref in value:
        _require(ref in index, f'unknown {name} reference')
        if kinds is not None:
            _require(index[ref].get('kind') in kinds, f'wrong source kind for {name}')


def _unknown_answer(answer):
    raw = answer.get('raw')
    if answer.get('answerState') == 'unknown' or raw is None or raw == []:
        return True
    return isinstance(raw, str) and raw.strip() in {'', '不清楚', '不知道', '未知', '不确定', '无法确定', '记不清'}


def validate_review(case, analysis, catalog, knowledge):
    """Validate trace links and state consistency, never medical causality."""
    review = analysis.get('review')
    _shape(review, 'version targetProblems chains', 'review')
    _require(review['version'] == VERSION, 'unsupported review version')
    facts = _index(case.get('evidence', []), 'personal evidence')
    history = _index(case.get('historicalInterpretations', []), 'historical interpretation')
    _require(not set(facts).intersection(history), 'historical interpretation overlaps personal evidence')
    for item in history.values():
        _require(item.get('kind') == 'historical_ai_interpretation', 'invalid historical interpretation kind')
        for field in ('text', 'sourceArtifact', 'pointer'):
            _text(item.get(field), f'historical {field}')
    sources = _index(knowledge.get('sources', []), 'knowledge source')
    concepts = _index(catalog.get('concepts', []), 'concept')
    risks = _index(analysis.get('risks', []), 'risk')
    targets = _index(review['targetProblems'], 'target problem')
    _require(bool(targets), 'review needs target problems')
    for target in targets.values():
        _shape(target, 'id label kind sourceRecordRefs historicalInterpretationRefs interpretation', 'target problem')
        _text(target['label'], 'target label')
        _text(target['interpretation'], 'target interpretation')
        _require(target['kind'] in {'measurement', 'symptom', 'recorded_diagnosis'}, 'invalid target kind')
        _refs(target['sourceRecordRefs'], facts, 'target source', nonempty=True, kinds=FACT_KINDS)
        _refs(target['historicalInterpretationRefs'], history, 'historical interpretation')
        if target['kind'] == 'measurement':
            _require(
                any(facts[ref]['kind'] == 'report_extraction' for ref in target['sourceRecordRefs']),
                'measurement target needs a report extraction',
            )

    factor_by_pair = {}
    for factor in analysis.get('factors', []):
        pair = (factor.get('conceptId'), factor.get('targetRiskId'))
        _require(pair not in factor_by_pair, 'duplicate factor pair')
        factor_by_pair[pair] = factor
    _require(bool(factor_by_pair), 'review needs existing factors')
    chains = _index(review['chains'], 'candidate', key='candidateId')
    covered = set()
    for chain in chains.values():
        _shape(
            chain,
            'candidateId conceptId targetRiskId targetProblemIds '
            'initialCandidate conditions answerEffects finalAssessment',
            'chain',
        )
        pair = (chain['conceptId'], chain['targetRiskId'])
        _require(pair in factor_by_pair and pair not in covered, 'chain must match exactly one factor pair')
        covered.add(pair)
        _require(chain['conceptId'] in concepts and chain['targetRiskId'] in risks, 'unknown chain concept or risk')
        factor = factor_by_pair[pair]
        _refs(factor.get('evidenceRefs', []), facts, 'factor evidence', kinds=FACT_KINDS)
        _refs(factor.get('counterEvidenceRefs', []), facts, 'factor counter evidence', kinds=FACT_KINDS)
        _refs(chain['targetProblemIds'], targets, 'target problem', nonempty=True)
        risk_refs = risks[chain['targetRiskId']].get('evidenceRefs', [])
        _refs(risk_refs, facts, 'risk evidence', nonempty=True, kinds=FACT_KINDS)
        for target_id in chain['targetProblemIds']:
            _require(
                bool(set(targets[target_id]['sourceRecordRefs']).intersection(risk_refs)),
                'target problem and risk evidence do not intersect',
            )
        initial = chain['initialCandidate']
        _shape(initial, 'hypothesis basis knowledgeLinks reportEvidenceRefs', 'initial candidate')
        _text(initial['hypothesis'], 'candidate hypothesis')
        _text(initial['basis'], 'candidate basis')
        _refs(initial['reportEvidenceRefs'], facts, 'candidate report evidence', kinds=REPORT_KINDS)
        _require(
            isinstance(initial['knowledgeLinks'], list) and bool(initial['knowledgeLinks']), 'missing knowledge links'
        )
        for link in initial['knowledgeLinks']:
            _shape(link, 'sourceId quote meaning applicability limitations', 'knowledge link')
            for field in ('sourceId', 'quote', 'meaning', 'applicability', 'limitations'):
                _text(link[field], f'knowledge {field}')
            _require(link['sourceId'] in sources, 'unknown knowledge source reference')
            source = sources[link['sourceId']]
            _require(
                source.get('provenance') in {'public_source_excerpt', 'evimed_original_text'},
                'knowledge link requires source text, not an AI summary',
            )
            _require(
                isinstance(source.get('sourceText'), str) and link['quote'] in source['sourceText'],
                'knowledge quote is not in source text',
            )
        conditions = _index(chain['conditions'], 'condition')
        _require(bool(conditions), 'candidate needs verification conditions')
        for condition in conditions.values():
            _shape(condition, 'id label initialState finalState evidenceRefs', 'condition')
            _text(condition['label'], 'condition label')
            _require(
                condition['initialState'] in STATES and condition['finalState'] in STATES,
                'invalid condition state',
            )
            _refs(condition['evidenceRefs'], facts, 'condition evidence', kinds=FACT_KINDS)
            if condition['finalState'] != 'unknown':
                _require(
                    any(
                        facts[ref]['kind'] != 'user_answer' or not _unknown_answer(facts[ref])
                        for ref in condition['evidenceRefs']
                    ),
                    'known condition needs substantive personal evidence',
                )
        _require(isinstance(chain['answerEffects'], list), 'answer effects must be a list')
        current_states = {key: value['initialState'] for key, value in conditions.items()}
        seen_effects = set()
        for effect in chain['answerEffects']:
            _shape(effect, 'conditionId answerRef before after effect reason', 'answer effect')
            _text(effect['reason'], 'answer effect reason')
            condition_id, answer_ref = effect['conditionId'], effect['answerRef']
            _require(condition_id in conditions, 'unknown answer effect condition')
            _refs([answer_ref], facts, 'answer effect', kinds={'user_answer'})
            _require((condition_id, answer_ref) not in seen_effects, 'duplicate condition and answer effect')
            seen_effects.add((condition_id, answer_ref))
            answer = facts[answer_ref]
            _text(answer.get('questionRef'), 'answer question reference')
            _text(answer.get('question'), 'answer question text')
            _require(
                answer_ref in conditions[condition_id]['evidenceRefs'], 'effect answer missing from condition evidence'
            )
            _require(effect['before'] == current_states[condition_id], 'answer effect before state does not match')
            _require(effect['after'] in STATES, 'invalid answer effect after state')
            _require(effect['effect'] in EFFECT_LABELS, 'invalid answer effect type')
            if _unknown_answer(answer):
                _require(
                    effect['effect'] == 'no_change' and effect['before'] == effect['after'],
                    'unknown answer cannot establish or change a condition',
                )
            if effect['effect'] == 'no_change':
                _require(effect['before'] == effect['after'], 'no_change must preserve the condition')
            else:
                _require(effect['before'] != effect['after'], 'changed effect must change the condition')
                expected = {'supports': {'present'}, 'weakens': {'absent', 'unknown'}, 'conflicts': {'conflicting'}}
                _require(effect['after'] in expected[effect['effect']], 'answer effect has incompatible after state')
                _require(
                    {effect['before'], effect['after']} != {'present', 'absent'},
                    'contradictory known states must preserve conflict',
                )
            current_states[condition_id] = effect['after']
        for key, condition in conditions.items():
            _require(
                current_states[key] == condition['finalState'], 'final condition is not explained by answer effects'
            )
        final = chain['finalAssessment']
        _shape(final, 'conclusion confirmed notEstablished nextVerification', 'final assessment')
        for field, value in final.items():
            _text(value, f'final {field}')
        _require(final['conclusion'] == factor.get('interpretation'), 'review conclusion differs from final factor')
        # These states concern presence and relationship support separately. An
        # unknown secondary condition does not automatically refute a risk relation.
        if factor.get('judgment') == 'supported':
            _require(
                factor.get('presence') == 'present', 'supported factor cannot have unknown or conflicting presence'
            )
    _require(covered == set(factor_by_pair), 'review must cover every factor exactly once')
    return review


def _record(record):
    result = deepcopy(record)
    result['sourceLabel'] = (
        '问卷原始回答' if record['kind'] == 'user_answer' else '报告解读 JSON 记录（未核验原始体检文件）'
    )
    result['sourcePointer'] = (
        record.get('pointer') or record.get('sourcePointer') or record['id'].removeprefix('report:')
    )
    return result


def build_review_projection(result):
    """Expand auditable references while preserving the supplied result."""
    case, analysis, knowledge, catalog = (result[key] for key in ('case', 'analysis', 'knowledge', 'catalog'))
    review = validate_review(case, analysis, catalog, knowledge)
    facts = {row['id']: row for row in case['evidence']}
    history = {row['id']: row for row in case.get('historicalInterpretations', [])}
    sources = {row['id']: row for row in knowledge['sources']}
    concepts = {row['id']: row for row in catalog['concepts']}
    risks = {row['id']: row for row in analysis['risks']}
    factors = {(row['conceptId'], row['targetRiskId']): row for row in analysis['factors']}
    targets = {}
    for row in review['targetProblems']:
        targets[row['id']] = {
            **deepcopy(row),
            'sourceRecords': [_record(facts[ref]) for ref in row['sourceRecordRefs']],
            'historicalInterpretations': [deepcopy(history[ref]) for ref in row['historicalInterpretationRefs']],
        }
    chains = []
    used_targets = set()
    for chain in review['chains']:
        row = deepcopy(chain)
        row['conceptLabel'] = concepts[chain['conceptId']]['label']
        row['factor'] = deepcopy(factors[(chain['conceptId'], chain['targetRiskId'])])
        row['factorEvidenceRecords'] = [_record(facts[ref]) for ref in row['factor'].get('evidenceRefs', [])]
        row['counterEvidenceRecords'] = [_record(facts[ref]) for ref in row['factor'].get('counterEvidenceRefs', [])]
        row['risk'] = deepcopy(risks[chain['targetRiskId']])
        row['targetProblems'] = [deepcopy(targets[ref]) for ref in chain['targetProblemIds']]
        used_targets.update(chain['targetProblemIds'])
        row['initialCandidate']['reportEvidenceRecords'] = [
            _record(facts[ref]) for ref in chain['initialCandidate']['reportEvidenceRefs']
        ]
        row['initialCandidate']['knowledgeLinks'] = [
            {**deepcopy(link), 'source': deepcopy(sources[link['sourceId']])}
            for link in chain['initialCandidate']['knowledgeLinks']
        ]
        conditions = {condition['id']: condition for condition in chain['conditions']}
        row['answerEffects'] = [
            {
                **deepcopy(effect),
                'conditionLabel': conditions[effect['conditionId']]['label'],
                'questionRef': facts[effect['answerRef']]['questionRef'],
                'question': facts[effect['answerRef']]['question'],
                'rawAnswer': deepcopy(facts[effect['answerRef']]['raw']),
                'alignment': facts[effect['answerRef']].get('alignment', 'unmatched'),
                'questionNameHint': facts[effect['answerRef']].get('questionNameHint'),
            }
            for effect in chain['answerEffects']
        ]
        chains.append(row)
    return {
        'version': VERSION,
        'traceBasis': {
            'mode': 'retrospective_reconstruction',
            'historicalPreAnswerRunAvailable': False,
            'analysisInputIncludedAnswers': True,
            'clinicalReview': 'pending',
        },
        'reportSourceAvailability': deepcopy(case.get('reportSourceAvailability', {})),
        'runMode': result.get('run', {}).get('mode'),
        'reportTime': case.get('reportTime'),
        'answeredAt': case.get('answeredAt'),
        'sourceFiles': deepcopy(case.get('sourceFiles', {})),
        'targetProblems': list(targets.values()),
        'unassignedTargetProblems': [deepcopy(target) for key, target in targets.items() if key not in used_targets],
        'chains': chains,
        'standardViews': deepcopy(result.get('views', [])),
    }


def _plain(value):
    if value is None:
        return '未提供'
    if isinstance(value, bool):
        return '是' if value else '否'
    if isinstance(value, dict):
        return '；'.join(f'{FIELD_LABELS.get(key, key)}：{_plain(item)}' for key, item in value.items())
    if isinstance(value, list):
        return '、'.join(_plain(item) for item in value) or '无'
    return str(value)


def _record_parts(record, *, compact_answer=False):
    """Keep the clinical record visible on one line and retain all extra fields."""
    raw = record.get('raw')
    context = record.get('context', {})
    date = None
    for key in ('observedAt', 'reportTime'):
        value = raw.get(key) if isinstance(raw, dict) else None
        inherited = context.get(key)
        if value is None and inherited is not None:
            value = inherited.get('value') if isinstance(inherited, dict) else inherited
        if value is not None:
            date = value
            break
    used = set()
    parts = []
    if record['kind'] == 'user_answer':
        if record.get('question') and not compact_answer:
            parts.append('问题：' + record['question'])
        parts.append('原始回答：' + _plain(raw))
    elif isinstance(raw, dict) and 'itemName' in raw:
        parts.append(
            _plain(raw['itemName'])
            + '：'
            + _plain(raw.get('resultValue'))
            + (' ' + _plain(raw['unit']) if raw.get('unit') else '')
        )
        used.update({'itemName', 'resultValue', 'unit'})
        for key in ('referenceRange', 'flag'):
            if raw.get(key) is not None:
                parts.append(FIELD_LABELS[key] + '：' + _plain(raw[key]))
                used.add(key)
    elif isinstance(raw, dict):
        selected = [key for key in ('findings', 'diagnosis') if key in raw] or list(raw)[:2]
        parts.extend(f'{FIELD_LABELS.get(key, key)}：{_plain(raw[key])}' for key in selected)
        used.update(selected)
    else:
        key = record['id'].rsplit('/', 1)[-1]
        parts.append(FIELD_LABELS.get(key, '原记录') + '：' + _plain(raw))
    if date is not None:
        parts.append('日期：' + _plain(date))
    extras = []
    if isinstance(raw, dict):
        extras.extend(f'{FIELD_LABELS.get(key, key)}：{_plain(value)}' for key, value in raw.items() if key not in used)
    for key, inherited in context.items():
        value = inherited.get('value') if isinstance(inherited, dict) else inherited
        pointer = inherited.get('pointer') if isinstance(inherited, dict) else None
        extras.append(f'{FIELD_LABELS.get(key, key)}：{_plain(value)}' + (f'（{pointer}）' if pointer else ''))
    return '；'.join(parts), f'定位：{record["id"]}', extras


def _record_lines(record, *, compact_answer=False):
    summary, locator, extras = _record_parts(record, compact_answer=compact_answer)
    return [summary, locator] + (['更多记录字段：' + '；'.join(extras)] if extras else [])


def _record_html(record, esc, *, compact_answer=False):
    summary, locator, extras = _record_parts(record, compact_answer=compact_answer)
    text = '<div class="record"><p>' + esc(summary) + '</p><p class="note">' + esc(locator) + '</p>'
    if extras:
        text += '<details><summary>更多记录字段</summary><p class="note">' + esc('；'.join(extras)) + '</p></details>'
    return text + '</div>'


def _target_html(target, esc):
    pieces = [f'<h4>{esc(target["label"])}</h4>']
    pieces.extend(_record_html(record, esc) for record in target['sourceRecords'])
    pieces.append('<p><strong>旧问卷解读文案（历史 AI 文案，不是个人事实）：</strong></p>')
    for historical in target['historicalInterpretations']:
        pieces.append(f'<blockquote>{esc(historical["text"])}</blockquote>')
        pieces.append(f'<p class="note">{esc(historical["sourceArtifact"])} · {esc(historical["pointer"])}</p>')
    if not target['historicalInterpretations']:
        pieces.append('<p class="note">没有关联的历史解读文案。</p>')
    pieces.append(f'<p><strong>本轮具体问题解读：</strong>{esc(target["interpretation"])}</p>')
    return ''.join(pieces)


def _target_markdown(target, esc):
    lines = [f'**{esc(target["label"])}**', '']
    for record in target['sourceRecords']:
        lines.extend('- ' + esc(line) for line in _record_lines(record))
        lines.append('')
    lines.extend(['旧问卷解读文案（历史 AI 文案，不是个人事实）：', ''])
    for historical in target['historicalInterpretations']:
        lines.extend(
            [
                esc(historical['text']),
                '',
                f'定位：{esc(historical["sourceArtifact"])} · {esc(historical["pointer"])}',
                '',
            ]
        )
    if not target['historicalInterpretations']:
        lines.extend(['没有关联的历史解读文案。', ''])
    lines.extend(['本轮具体问题解读：' + esc(target['interpretation']), ''])
    return lines


def _safe_url(value):
    if not isinstance(value, str) or re.search(r'[\s<>"\'\\]', value):
        return None
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme in {'http', 'https'} and parsed.netloc else None
    except ValueError:
        return None


def _source_lines(source):
    lines = [f'来源：{source.get("title", source["id"])}（{source["id"]}）']
    for key, label in (
        ('sourceType', '资料类型'),
        ('publishedAt', '发表日期'),
        ('retrievedAt', '取得日期'),
        ('locator', '原页章节'),
    ):
        lines.append(label + '：' + _plain(source.get(key)))
    grading = source.get('evidenceGrading', 'ungraded')
    lines.append('证据分级：' + ('未分级' if grading == 'ungraded' else _plain(grading)))
    if source.get('upstreamId') is not None:
        lines.append('上游记录 ID：' + str(source['upstreamId']))
    if source.get('provenance'):
        lines.append('来源方式：' + PROVENANCE_LABELS.get(source['provenance'], source['provenance']))
    lines.extend(f'{FIELD_LABELS.get(key, key)}：{_plain(value)}' for key, value in source.get('metadata', {}).items())
    return lines


def render_review_html(result):
    projection = build_review_projection(result)

    def esc(value):
        return html.escape(_plain(value), quote=True)

    pieces = [
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<title>因素分析 · 老板审阅版</title><style>',
        'body{max-width:1120px;margin:32px auto;padding:0 24px;color:#25333b;background:#fff;',
        'font:16px/1.8 system-ui}h1{font-size:28px}h2{margin-top:36px}h3{font-size:19px}',
        'article{border:1px solid #dfe6e8;border-radius:10px;padding:20px 24px;margin:24px 0}',
        '.note{color:#53636b}.record,.source{border-left:3px solid #c7d9d5;padding-left:16px;margin:14px 0}',
        'table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #dfe6e8;text-align:left;',
        'vertical-align:top;padding:10px}blockquote{margin:12px 0;padding:10px 16px;background:#f5f8f7}',
        'a{color:#16665b;overflow-wrap:anywhere}p{margin:8px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere}',
        '</style></head><body><h1>因素分析 · 老板审阅版</h1>',
        '<p class="note">医学审核：待完成。本次为回顾重建，分析输入已包含问卷答案；没有历史答前模型运行记录。</p>',
        '<p class="note">结果来源：'
        + esc(MODE_LABELS.get(projection['runMode'], '运行来源未提供；不能据此认定已真实调用模型或知识库'))
        + '。</p>',
        '<p class="note">报告解读 JSON 记录（未核验原始体检文件）。支持风险关系不等于确认个人病因。</p>',
        f'<p class="note">体检时间：{esc(projection["reportTime"])}；回答时间：{esc(projection["answeredAt"])}。</p>',
        '<h2>审阅概览</h2><table><tr><th>因素</th><th>具体问题</th><th>最终判断</th></tr>',
    ]
    source_notes = []
    availability = projection['reportSourceAvailability']
    if availability.get('note'):
        source_notes.append('<p class="note">' + esc(availability['note']) + '</p>')
    if availability.get('reportAiSummaryAvailable') is False:
        source_notes.append('<p class="note">所提供解读 JSON 没有 AI 风险总结；下列历史解读来自旧问卷承接文本。</p>')
    pieces[-1:-1] = source_notes
    for index, chain in enumerate(projection['chains'], 1):
        pieces.append(
            f'<tr><td><a href="#chain-{index}">{esc(chain["conceptLabel"])}</a></td>'
            f'<td>{esc("；".join(target["label"] for target in chain["targetProblems"]))}</td>'
            f'<td>{esc(JUDGMENT_LABELS.get(chain["factor"].get("judgment"), "待审阅"))}<br>'
            f'{esc(chain["finalAssessment"]["conclusion"])}</td></tr>'
        )
    pieces.append('</table>')
    if projection['unassignedTargetProblems']:
        pieces.append('<h2>尚未建立诱因链的问题</h2><p>以下问题保留原记录与解读，当前没有关联候选；不表示已排除。</p>')
        for target in projection['unassignedTargetProblems']:
            pieces.append('<article>' + _target_html(target, esc) + '</article>')
    for index, chain in enumerate(projection['chains'], 1):
        shown_records = set()
        pieces.append(f'<article id="chain-{index}"><h2>{index}. {esc(chain["conceptLabel"])}</h2>')
        pieces.append('<h3>一、具体问题、资料记录与解读</h3>')
        for target in chain['targetProblems']:
            pieces.append(_target_html(target, esc))
            shown_records.update(record['id'] for record in target['sourceRecords'])
        pieces.append(
            f'<p><strong>本轮风险解读：{esc(chain["risk"]["label"])}</strong> '
            f'{esc(chain["risk"]["interpretation"])}</p>'
        )
        for label, key in [('因素支持记录', 'factorEvidenceRecords'), ('因素反证或冲突记录', 'counterEvidenceRecords')]:
            pieces.append(f'<p><strong>{label}：</strong></p>')
            for record in chain[key]:
                if record['id'] in shown_records:
                    pieces.append(f'<p class="note">已展示记录：{esc(record["id"])}。</p>')
                else:
                    pieces.append(_record_html(record, esc, compact_answer=True))
                    shown_records.add(record['id'])
            if not chain[key]:
                pieces.append('<p class="note">没有关联记录。</p>')
        pieces.append('<h3>二、循证依据与回顾重建的初始候选</h3>')
        initial = chain['initialCandidate']
        for link in initial['knowledgeLinks']:
            source = link['source']
            pieces.append('<div class="source">' + ''.join(f'<p>{esc(line)}</p>' for line in _source_lines(source)))
            url = _safe_url(source.get('url'))
            if url:
                pieces.append(f'<p><a href="{html.escape(url, quote=True)}">查看来源</a> · {esc(url)}</p>')
            pieces.append(f'<blockquote>{esc(link["quote"])}</blockquote>')
            for label, key in [('中文含义', 'meaning'), ('适用范围', 'applicability'), ('证据限制', 'limitations')]:
                pieces.append(f'<p><strong>{label}：</strong>{esc(link[key])}</p>')
            pieces.append(
                f'<details><summary>本次保存的知识原文</summary><pre>{esc(source["sourceText"])}</pre></details></div>'
            )
        pieces.append(f'<p><strong>初始候选（回顾重建）：</strong>{esc(initial["hypothesis"])}</p>')
        pieces.append(f'<p><strong>列入依据：</strong>{esc(initial["basis"])}</p>')
        for record in initial['reportEvidenceRecords']:
            if record['id'] in shown_records:
                pieces.append(f'<p class="note">候选关联的已展示报告记录：{esc(record["id"])}。</p>')
            else:
                pieces.append(_record_html(record, esc))
                shown_records.add(record['id'])
        pieces.append('<h3>三、具体问题、原始回答与条件变化</h3>')
        pieces.append('<table><tr><th>核实条件</th><th>初始状态（重建）</th><th>结合答案后</th></tr>')
        for condition in chain['conditions']:
            pieces.append(
                f'<tr><td>{esc(condition["label"])}</td><td>{esc(STATE_LABELS[condition["initialState"]])}</td><td>{esc(STATE_LABELS[condition["finalState"]])}</td></tr>'
            )
        pieces.append('</table>')
        if not chain['answerEffects']:
            pieces.append('<p>没有关联的答案状态变化；既有条件状态保持。</p>')
        for effect in chain['answerEffects']:
            pieces.append(
                f'<p><strong>问题：</strong>{esc(effect["question"])}</p><p><strong>原始回答：</strong>{esc(effect["rawAnswer"])}</p>'
            )
            pieces.append(
                f'<p class="note">题目定位：{esc(effect["questionRef"])}；'
                f'答案定位：{esc(effect["answerRef"])}；'
                f'题干映射：{esc(ALIGNMENT_LABELS.get(effect["alignment"], effect["alignment"]))}'
                '（不证明同一发布版本）。</p>'
            )
            pieces.append(
                f'<p><strong>{esc(effect["conditionLabel"])}：</strong>'
                f'{esc(STATE_LABELS[effect["before"]])} → {esc(STATE_LABELS[effect["after"]])}；'
                f'{esc(EFFECT_LABELS[effect["effect"]])}。{esc(effect["reason"])}</p>'
            )
        pieces.append('<h3>四、最终判断与核实边界</h3>')
        if chain['factor'].get('temporalMatch'):
            temporal = chain['factor']['temporalMatch']
            pieces.append(f'<p class="note">时间关系：{esc(TEMPORAL_LABELS.get(temporal, temporal))}。</p>')
        for label, key in [
            ('最终结论', 'conclusion'),
            ('已经确认', 'confirmed'),
            ('尚未建立', 'notEstablished'),
            ('下一步核实', 'nextVerification'),
        ]:
            pieces.append(f'<p><strong>{label}：</strong>{esc(chain["finalAssessment"][key])}</p>')
        pieces.append(
            '<p class="note">以上状态变化为本次回顾性证据解释，不是问卷提交当时的系统执行轨迹。</p></article>'
        )
    pieces.append(
        '<h2>附录：同一关系的三种视图</h2><table><tr><th>因素</th><th>临床表达</th>'
        '<th>MeSH 分类</th><th>关系语义示例</th></tr>'
    )
    for view in projection['standardViews']:
        pieces.append(
            f'<tr><td>{esc(view.get("clinical", {}).get("label"))}</td>'
            f'<td>{esc(view.get("clinical", {}).get("relation"))}</td>'
            f'<td>{esc(view.get("mesh", {}).get("relationHeading") or "未映射")}</td>'
            f'<td>{esc(view.get("snomedInspired", {}).get("relation"))}</td></tr>'
        )
    pieces.append(
        '</table><p class="note">MeSH 仅用于关系分类；关系语义示例未完成 SNOMED CT 编码。'
        '三种视图不是三套独立诊断。</p></body></html>'
    )
    return ''.join(pieces)


def render_review_markdown(result):
    projection = build_review_projection(result)

    def esc(value):
        return re.sub(r'([\\`*_{}\[\]<>|])', r'\\\1', html.escape(_plain(value), quote=False))

    lines = [
        '# 因素分析 · 老板审阅版',
        '',
        '医学审核：待完成。本次为回顾重建，分析输入已包含答案；没有历史答前模型运行记录。',
        '',
        '结果来源：'
        + esc(MODE_LABELS.get(projection['runMode'], '运行来源未提供；不能据此认定已真实调用模型或知识库')),
        '',
        '报告解读 JSON 记录（未核验原始体检文件）。支持风险关系不等于确认个人病因。',
        '',
        f'体检时间：{esc(projection["reportTime"])}；回答时间：{esc(projection["answeredAt"])}。',
        '',
        '## 审阅概览',
        '',
        '| 因素 | 具体问题 | 最终判断 |',
        '| --- | --- | --- |',
    ]
    source_notes = []
    availability = projection['reportSourceAvailability']
    if availability.get('note'):
        source_notes.extend([esc(availability['note']), ''])
    if availability.get('reportAiSummaryAvailable') is False:
        source_notes.extend(['所提供解读 JSON 没有 AI 风险总结；下列历史解读来自旧问卷承接文本。', ''])
    overview_position = lines.index('## 审阅概览')
    lines[overview_position:overview_position] = source_notes
    for chain in projection['chains']:
        lines.append(
            f'| {esc(chain["conceptLabel"])} | '
            f'{esc("；".join(t["label"] for t in chain["targetProblems"]))} | '
            f'{esc(chain["finalAssessment"]["conclusion"])} |'
        )
    if projection['unassignedTargetProblems']:
        lines.extend(
            ['', '## 尚未建立诱因链的问题', '', '以下问题保留原记录与解读，当前没有关联候选；不表示已排除。', '']
        )
        for target in projection['unassignedTargetProblems']:
            lines.extend(_target_markdown(target, esc))
    for index, chain in enumerate(projection['chains'], 1):
        shown_records = set()
        lines.extend(['', f'## {index}. {esc(chain["conceptLabel"])}', '', '### 一、具体问题、资料记录与解读', ''])
        for target in chain['targetProblems']:
            lines.extend(_target_markdown(target, esc))
            shown_records.update(record['id'] for record in target['sourceRecords'])
        lines.extend(['本轮风险解读：' + esc(chain['risk']['label']) + '；' + esc(chain['risk']['interpretation']), ''])
        for label, key in [('因素支持记录', 'factorEvidenceRecords'), ('因素反证或冲突记录', 'counterEvidenceRecords')]:
            lines.extend([label + '：', ''])
            for record in chain[key]:
                if record['id'] in shown_records:
                    lines.append('已展示记录：' + esc(record['id']))
                else:
                    lines.extend('- ' + esc(line) for line in _record_lines(record, compact_answer=True))
                    shown_records.add(record['id'])
                lines.append('')
            if not chain[key]:
                lines.extend(['没有关联记录。', ''])
        lines.extend(['### 二、循证依据与回顾重建的初始候选', ''])
        initial = chain['initialCandidate']
        for link in initial['knowledgeLinks']:
            source = link['source']
            lines.extend('- ' + esc(line) for line in _source_lines(source))
            url = _safe_url(source.get('url'))
            if url:
                lines.append(f'- [查看来源](<{url}>)')
            lines.extend(
                [
                    '',
                    '原文短引：' + esc(link['quote']),
                    '',
                    '中文含义：' + esc(link['meaning']),
                    '',
                    '适用范围：' + esc(link['applicability']),
                    '',
                    '证据限制：' + esc(link['limitations']),
                    '',
                ]
            )
        lines.extend(
            ['初始候选（回顾重建）：' + esc(initial['hypothesis']), '', '列入依据：' + esc(initial['basis']), '']
        )
        for record in initial['reportEvidenceRecords']:
            if record['id'] in shown_records:
                lines.append('候选关联的已展示报告记录：' + esc(record['id']))
            else:
                lines.extend('- ' + esc(line) for line in _record_lines(record))
                shown_records.add(record['id'])
            lines.append('')
        lines.extend(
            [
                '### 三、具体问题、原始回答与条件变化',
                '',
                '| 核实条件 | 初始状态（重建） | 结合答案后 |',
                '| --- | --- | --- |',
            ]
        )
        for condition in chain['conditions']:
            lines.append(
                f'| {esc(condition["label"])} | {STATE_LABELS[condition["initialState"]]} | '
                f'{STATE_LABELS[condition["finalState"]]} |'
            )
        lines.append('')
        if not chain['answerEffects']:
            lines.extend(['没有关联的答案状态变化；既有条件状态保持。', ''])
        for effect in chain['answerEffects']:
            lines.extend(
                [
                    '问题：' + esc(effect['question']),
                    '',
                    '原始回答：' + esc(effect['rawAnswer']),
                    '',
                    f'定位：{esc(effect["questionRef"])}；{esc(effect["answerRef"])}；'
                    f'题干映射：{esc(ALIGNMENT_LABELS.get(effect["alignment"], effect["alignment"]))}'
                    '（不证明同一发布版本）。',
                    '',
                    f'{esc(effect["conditionLabel"])}：{STATE_LABELS[effect["before"]]} → '
                    f'{STATE_LABELS[effect["after"]]}；{EFFECT_LABELS[effect["effect"]]}。{esc(effect["reason"])}',
                    '',
                ]
            )
        lines.extend(['### 四、最终判断与核实边界', ''])
        if chain['factor'].get('temporalMatch'):
            temporal = chain['factor']['temporalMatch']
            lines.extend(['时间关系：' + esc(TEMPORAL_LABELS.get(temporal, temporal)), ''])
        for label, key in [
            ('最终结论', 'conclusion'),
            ('已经确认', 'confirmed'),
            ('尚未建立', 'notEstablished'),
            ('下一步核实', 'nextVerification'),
        ]:
            lines.extend([f'**{label}：**' + esc(chain['finalAssessment'][key]), ''])
        lines.extend(['以上状态变化为本次回顾性证据解释，不是问卷提交当时的系统执行轨迹。', ''])
    lines.extend(
        [
            '## 附录：同一关系的三种视图',
            '',
            '| 因素 | 临床表达 | MeSH 分类 | 关系语义示例 |',
            '| --- | --- | --- | --- |',
        ]
    )
    for view in projection['standardViews']:
        lines.append(
            f'| {esc(view.get("clinical", {}).get("label"))} | '
            f'{esc(view.get("clinical", {}).get("relation"))} | '
            f'{esc(view.get("mesh", {}).get("relationHeading") or "未映射")} | '
            f'{esc(view.get("snomedInspired", {}).get("relation"))} |'
        )
    lines.extend(['', 'MeSH 仅用于关系分类；关系语义示例未完成 SNOMED CT 编码。三种视图不是三套独立诊断。', ''])
    return '\n'.join(lines)
