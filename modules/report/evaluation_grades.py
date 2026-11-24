"""Private checks for literal prose claims about typed report grades.

This module does not calculate grades or interpret source labels/ranges. It
recognizes a small set of explicit named assertions and compares each item
only with its own, source-bound, report-calculated display grade.
"""

from __future__ import annotations

from collections.abc import Sequence
import re
from typing import Any
import unicodedata

from modules.report.schemas import ReportSourceFact


_LEVELS = frozenset({'强', '弱', '较弱', '正常', '极低', '偏低', '偏高', '正常-弱', '正常-偏低'})
_GRADE = re.compile(r'正常-(?:偏低|弱)|极低|偏低|偏高|较弱|(?<![较稍偏减衰脆虚])弱(?![化点项势])|(?<![增加较稍偏])强(?![化调度])|正常(?![人菌值范围区间])')
_JOIN = re.compile(r'(?:(?:[、，,]|和|及|以及|与|等|还有|均|皆|都)'
    r'(?:部分|某些|一些|多种|多项|少数|多数|全部)*)+')
_PREFIX_JOIN = re.compile(r'(?:、|和|及|以及|与|等|还有)+')
# Deliberately finite nominal/predicate vocabulary: an intervening clinical
# interpretation or an unrelated named assertion must not become a grade link.
_POST_LINK = re.compile(
    r'(?:的|其|这一项|该项|相关|结果|项目|指标|功能|营养|神经递质|菌群|'
    r'合成|代谢|支持|或|与|和|能力|估计|评估|分值|评分|得分|分级|等级|判定|报告|本次|本报告|该报告|'
    r'在|中|按|按照|标准|方法学|固定|显示|表现|呈现|呈|对应|保留|属于|属|列入|列|评定|评|判|'
    r'被|归入|归|划分|标记|标注|标|处于|为|是|等|均|皆|都|则|'
    r'[“”\"\'「」『』:：，,])*'
)
_PRE_LINK = re.compile(
    r'(?:的)?(?:项目|指标|结果|分级|等级|功能|能力|合成能力|代谢能力|菌群功能)*'
    r'(?:有|包括|包含|为|是|[:：])(?:[“”\"\'「」『』])*'
)
_NEGATION = re.compile(
    r'不能|不可|无法|不足以|不应|不宜|未能|尚未|未必|不一定|并非|不是|不属|'
    r'不代表|不等于|不等同|不表示|不意味|未显示|未见|没有显示|无证据|缺乏证据|'
    r'是否|可能|或许|假如|如果|(?:^|[，,])若'
)
_SOURCE = re.compile(r'原(?:始)?报告|来源(?:报告|分级|状态|标记|标签)|原(?:分级|标记|标签)')
_REPORT = re.compile(r'本(?:次)?报告|本次评估|固定(?:方法学|评估|分级)?标准')
_REFERENCE = re.compile(r'按(?:照)?(?:原(?:始)?报告|来源(?:报告)?|所印)(?:的)?(?:区间|图例|参考范围)(?:对照|分级)?')
_ALTERNATIVE = re.compile(r'^(?:[“”\"\'「」『』])*(?:或|和|、|至|到|/)(?:正常-(?:偏低|弱)|极低|偏低|偏高|较弱|弱|强|正常)')


def _normalized(text: str) -> str:
    text = unicodedata.normalize('NFKC', text).casefold()
    text = re.sub(r'\s+', '', text).translate(str.maketrans({'‐': '-', '‑': '-', '–': '-', '—': '-'}))
    # Optional punctuation in a Greek-letter chemical name is typographic,
    # not an inferred alias (e.g. γ-氨基丁酸 / γ氨基丁酸).
    return re.sub(r'(?<=[α-ω])-(?=[\u4e00-\u9fff])', '', text)


def _metric_name_variants(name: str) -> tuple[str, ...]:
    """Omit display suffixes/prefixes only within one bound metric's name."""
    literal = _normalized(name)
    shortened = re.sub(r'(?:评分|分值|得分|评估|指数)$', '', literal)
    variants = [literal]
    if shortened != literal and len(shortened) >= 2:
        variants.append(shortened)
        unqualified = re.sub(r'^(?:肠道菌群|肠道微生物组|肠道微生态|菌群)', '', shortened)
        if len(unqualified) >= 2:
            variants.append(unqualified)
    return tuple(dict.fromkeys(variants))


def _last_marker(pattern: re.Pattern[str], text: str) -> int:
    return max((match.start() for match in pattern.finditer(text)), default=-1)


def _original_source_claim(prefix: str) -> bool:
    return _last_marker(_SOURCE, prefix) > _last_marker(_REPORT, prefix)


def _list_before(names: list[re.Match[str]], end: int, text: str) -> list[re.Match[str]]:
    candidates = [name for name in names if name.end() <= end]
    if not candidates:
        return []
    result = [candidates[-1]]
    for name in reversed(candidates[:-1]):
        if not _JOIN.fullmatch(text[name.end():result[0].start()]):
            break
        result.insert(0, name)
    return result


def _list_after(names: list[re.Match[str]], start: int, text: str) -> list[re.Match[str]]:
    candidates = [name for name in names if name.start() >= start]
    if not candidates:
        return []
    result = [candidates[0]]
    for name in candidates[1:]:
        if not _PREFIX_JOIN.fullmatch(text[result[-1].end():name.start()]):
            break
        result.append(name)
    return result


def _expand_vitamin_list_names(sentence: str, names: Sequence[str]) -> str:
    """Resolve an omitted vitamin prefix only inside an explicit named list.

    This is typographic expansion against existing context names, not a new
    alias or grading authority. A bare B1 identifier elsewhere in a sentence
    must not borrow the vitamin's bound result.
    """
    suffixes = {name.removeprefix('维生素'): name for name in names
        if re.fullmatch(r'维生素(?:b\d{1,2}|[acdek]\d?)', name)
        and name.removeprefix('维生素') not in names}
    if not suffixes or '维生素' not in sentence:
        return sentence
    bare = r'(?<![a-z0-9])(?:' + '|'.join(re.escape(suffix)
        for suffix in sorted(suffixes, key=len, reverse=True)) + r')(?![a-z0-9])'
    token = re.compile('|'.join(re.escape(name)
        for name in sorted(names, key=len, reverse=True)) + '|' + bare)
    matches = list(token.finditer(sentence))
    groups: list[list[re.Match[str]]] = []
    for match in matches:
        if groups and _JOIN.fullmatch(sentence[groups[-1][-1].end():match.start()]):
            groups[-1].append(match)
        else:
            groups.append([match])
    replacements = []
    for group in groups:
        explicit = any(match.group().startswith('维生素') for match in group)
        # Also accept a literal list heading, e.g. "维生素：B1、B12".
        if not explicit and not re.search(r'维生素[:：]$', sentence[:group[0].start()]):
            continue
        replacements.extend((match.start(), match.end(), suffixes[match.group()])
            for match in group if match.group() in suffixes)
    for start, end, replacement in reversed(replacements):
        sentence = sentence[:start] + replacement + sentence[end:]
    return sentence


def grade_statement_failures(
    text: str,
    facts: list[ReportSourceFact],
    context: Sequence[dict[str, Any]],
) -> list[str]:
    """Reject explicit named report grades that contradict their typed rows.

    Unknown grades/origins and unbound rows supply no grading authority.
    Names are matched longest first, including ineligible context names, so a
    shorter item cannot borrow a longer item's assertion or another item's ID.
    Original-source attributions, negation, alternatives and unnamed group
    summaries remain with the existing source/clinical guards.
    """
    bound = {fact.fact_id for fact in facts}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for row in context:
        name = row.get('name')
        if isinstance(name, str) and (key := _normalized(name)):
            for variant in _metric_name_variants(name):
                by_name.setdefault(variant, []).append(row)
            # These spellings are literal abbreviations of the same ratio,
            # not grading thresholds or aliases for another measurement.
            for abbreviation in re.findall(r'F/B|B/E', name, re.I):
                for alias in (abbreviation, abbreviation + '比值'):
                    by_name.setdefault(_normalized(alias), []).append(row)
        if row.get('semanticType') == 'nutrientGroup':
            # A group may share a predicate with named measured items. Its
            # aliases are list boundaries, never grading authority for them.
            for alias in row.get('aliases', []):
                if isinstance(alias, str) and (key := _normalized(alias)):
                    by_name.setdefault(key, []).append(row)
    contextual_ids = {fid for rows in by_name.values() for row in rows for fid in row.get('factIds') or ()}
    for fact in facts:
        if (fact.fact_id in contextual_ids or fact.kind not in {'function','clinical'} or fact.status
                or fact.reference_range or not re.fullmatch(
                    r'[<>≤≥]?\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)', fact.value.strip())):
            continue
        row = {'name':fact.name, 'value':fact.value, 'unit':fact.unit, 'sourceStatus':None,
            'assessmentLevel':None, 'assessmentOrigin':None, 'factIds':[fact.fact_id]}
        aliases = [fact.name, *(alias for token in re.findall(r'F/B|B/E', fact.name, re.I)
            for alias in (token, token + '比值'))]
        for name in aliases:
            for variant in _metric_name_variants(name):
                by_name.setdefault(variant, []).append(row)
    if not by_name or not bound:
        return []

    expected = {}
    unresolved = {}
    ungraded = {}
    for name, rows in by_name.items():
        eligible = [row for row in rows
            if row.get('assessmentOrigin') in {'source', 'report', 'reference'}
            and row.get('assessmentLevel') in _LEVELS
            and row.get('value') is not None
            and bound.intersection(row.get('factIds') or [])]
        levels = {row['assessmentLevel'] for row in eligible}
        if len(levels) == 1:
            expected[name] = (eligible[0]['name'], levels.pop(), eligible[0]['assessmentOrigin'])
        elif not eligible:
            # A measured row with a printed range but no resolved grade is
            # different from an unrelated/unbound row. Do not silently pick
            # a band for an overlapping endpoint in the authored prose.
            measured = [row for row in rows if row.get('value') is not None
                and row.get('assessmentOrigin') not in {'source', 'report', 'reference'}
                and (row.get('assessmentLevel') in (None, '') or not row.get('referenceRange'))
                and row.get('sourceStatus') in (None, '')
                and re.fullmatch(r'[<>≤≥]?\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)', str(row['value']).strip())
                and bound.intersection(row.get('factIds') or [])]
            if measured:
                unresolved[name] = measured[0]['name']
                ungraded[name] = measured
    if not expected and not unresolved:
        return []

    name_pattern = re.compile('|'.join(re.escape(name) for name in sorted(by_name, key=len, reverse=True)))
    failures = []
    seen = set()

    def check(subjects: list[re.Match[str]], grade: str, source_claim: bool = False) -> None:
        for subject in subjects:
            if subject.group() in unresolved:
                name = unresolved[subject.group()]
                # A paper's general association is not an interpretation of
                # this person's unlabeled number. Patient/source attribution
                # or an explicit measured value still makes it a local claim.
                personal = bool(re.search(r'本次|本例|本人|您的|受检者|该患者|原报告|来源报告|实际结果', sentence))
                numerical = any(re.search(r'(?<![\d.])' + re.escape(_normalized(str(row['value'])))
                    + r'(?![\d.])', sentence[subject.end():]) for row in ungraded[subject.group()])
                general = bool(re.search(r'研究|文献|队列|人群|试验|与[^。；;]{0,30}(?:相关|关联)', sentence))
                if general and not personal and not numerical and not source_claim:
                    continue
                if (name, grade, 'unresolved') not in seen:
                    seen.add((name, grade, 'unresolved'))
                    failures.append(f'{name}已绑定的连续实测值尚无确定单项分级，正文不能新增“{grade}”档位；保留原值及原有方向')
                continue
            if subject.group() not in expected:
                continue
            name, level, origin = expected[subject.group()]
            if source_claim and origin == 'report':
                continue  # Preserve independently printed source labels.
            if source_claim and origin == 'reference':
                if (name, grade, 'source') not in seen:
                    seen.add((name, grade, 'source'))
                    failures.append(f'{name}的等级来自原报告区间对照，不能写成原件直接印出的单项状态；准确说明区间对照依据')
                continue
            if grade != level and (name, grade, level) not in seen:
                seen.add((name, grade, level))
                failures.append(f'{name}的正文分级“{grade}”与该项已绑定的分级“{level}”不一致；使用该项实际分级及其依据')

    def measured_link(link, rows):
        for row in rows:
            stripped = link.replace(_normalized(str(row['value'])), '')
            for number in re.finditer(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)', link):
                try:
                    if float(number.group()) == float(row['value']):
                        stripped = link[:number.start()] + link[number.end():]
                        break
                except (TypeError, ValueError):
                    pass
            if row.get('unit'):
                stripped = stripped.replace(_normalized(str(row['unit'])), '')
            if _POST_LINK.fullmatch(stripped):
                return True
        return False

    for raw_sentence in re.split(r'[。！？!?；;\n]', text):
        sentence = _normalized(raw_sentence).replace('等对应的菌群功能', '等')
        sentence = _expand_vitamin_list_names(sentence, tuple(by_name))
        names = list(name_pattern.finditer(sentence))
        grades = [grade for grade in _GRADE.finditer(sentence)
            if not any(name.start() <= grade.start() < name.end() for name in names)]
        # A qualitative synonym cannot supply a missing grade either. Keep
        # this addition local to ungraded metrics; existing printed labels
        # and the approved finite grading vocabulary are unchanged.
        for grade in re.finditer(r'偏弱|减弱|低下|不足|薄弱|弱项|短板', sentence):
            if any(name.group() in ungraded and name.end() <= grade.start()
                    and (_POST_LINK.fullmatch(sentence[name.end():grade.start()])
                        or measured_link(sentence[name.end():grade.start()], ungraded[name.group()])) for name in names):
                grades.append(grade)
        grades.sort(key=lambda grade: (grade.start(), -len(grade.group())))
        prefix_subjects: set[int] = set()
        for index, grade in enumerate(grades):
            # "弱或较弱" is an alternative, not one exact display assertion.
            if _ALTERNATIVE.match(sentence[grade.end():]) or (index and _ALTERNATIVE.match(
                    sentence[grades[index - 1].end():]) and grades[index - 1].end() < grade.start()):
                continue
            prefix = sentence[:grade.end()]
            source_claim = _original_source_claim(prefix) and not _REFERENCE.search(prefix)
            previous_end = grades[index - 1].end() if index else 0
            subjects = _list_before([name for name in names
                if name.start() >= previous_end and name.start() not in prefix_subjects], grade.start(), sentence)
            link = sentence[subjects[-1].end():grade.start()] if subjects else ''
            measured = bool(subjects and subjects[-1].group() in ungraded
                and measured_link(link, ungraded[subjects[-1].group()]))
            if subjects and (_POST_LINK.fullmatch(link) or measured):
                # A comma can be a list separator. Negation before the first
                # subject belongs to this assertion; later limitations do not.
                start = max(sentence.rfind('，', 0, subjects[0].start()), sentence.rfind(',', 0, subjects[0].start())) + 1
                if not _NEGATION.search(sentence[start:grade.start()]):
                    check(subjects, grade.group(), source_claim)
            following = _list_after(names, grade.end(), sentence)
            prefix_link = sentence[grade.end():following[0].start()] if following else ''
            ungraded_prefix = (following and following[0].group() in ungraded
                and re.fullmatch(r'(?:的|这一项|该项)*', prefix_link))
            if following and (_PRE_LINK.fullmatch(prefix_link) or ungraded_prefix):
                start = max(sentence.rfind('，', 0, grade.start()), sentence.rfind(',', 0, grade.start())) + 1
                if not _NEGATION.search(sentence[start:grade.start()]):
                    check(following, grade.group(), source_claim)
                    prefix_subjects.update(name.start() for name in following)
    return failures
