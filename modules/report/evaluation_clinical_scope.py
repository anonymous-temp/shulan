"""Source prerequisites for specimen-specific clinical interpretation.

A positive occult-blood result establishes a positive result. Its name alone
does not establish a stool test or eligibility for a colorectal-screening
pathway, even when a correctly quoted guideline describes that pathway.
"""

from copy import deepcopy
import re

from .schemas import ReportSourceFact


_OCCULT = re.compile(r'隐血|潜血|\b(?:occult[ -]*blood|FOBT|FIT)\b', re.I)
_STOOL = re.compile(r'大便|粪便|粪样|便常规|\b(?:stool|faecal|fecal|faeces|feces|FOBT|FIT)\b', re.I)
_COLORECTAL = re.compile(r'结肠镜|肠镜|结直肠(?:癌|肿瘤)?筛查|大肠癌筛查|粪便筛查|\b(?:colonoscopy|colorectal\s+(?:cancer\s+)?screening|FOBT|FIT)\b', re.I)
_NEGATION = re.compile(r'不能|不应|不可|不宜|不足以|不套用|不作为|并非|不是|未(?:注明|提供|明确)|没有(?:提供|注明|明确)')


def aging_clinical_facts(facts: list[ReportSourceFact]) -> list[ReportSourceFact]:
    """Select explicit ageing/bone findings without interpreting a plain film.

    A source bone-density/metabolism record may be normal or abnormal. A
    routine normal joint film alone is not a bone-density observation. Actual
    fractures and other affirmative bone/joint disease remain clinical clues,
    without acquiring a fragility-fracture or osteoporosis diagnosis.
    """
    direct = re.compile(r'衰老|老年综合征|衰弱|肌少症|骨质疏松|骨质减少|骨密度|骨量|骨代谢|骨转换|'
        r'动脉粥样硬化|认知(?:功能)?下降|骨钙素|骨碱性磷酸酶|\b(?:BMD|DXA|DEXA|P1NP|PINP)\b|'
        r'osteoporo|osteopeni|frailty|sarcopenia|atherosclerosis|cognitive\s+decline', re.I)
    bone_finding = re.compile(r'骨折|骨关节炎|关节炎|退行性(?:病变|改变|变)|骨质增生|骨赘|fracture|osteoarthritis', re.I)
    result = []
    for fact in facts:
        if (fact.kind not in {'clinical', 'self_reported'} or not fact.value.strip()
                or fact.value.strip() in {'未提供', '未提供数值', '待提供', '--', 'N/A'}):
            continue
        text = fact.name + ' ' + fact.value
        if direct.search(text) or any(re.search(r'(?:无|非)[^，,。；;]{0,3}外伤[^，,。；;]{0,6}$', text[:match.start()])
                or not re.search(r'(?:未见|无|否认|未发现|不支持|排除|no|without)[^，,。；;但]{0,60}$', text[:match.start()], re.I)
                for match in bone_finding.finditer(text)):
            result.append(fact)
    return result

# These are routes, not diagnoses. Mentioning tissue confirmation does not
# establish how tissue should be obtained, just as generic imaging does not
# establish a particular imaging examination.
_PROCEDURE_ROUTES = tuple((label, re.compile(pattern, re.I)) for label, pattern in (
    ('内镜', r'内镜|(?<!结)肠镜|endoscop|gastroscop|colonoscop|enteroscop|(?<![A-Za-z])(?:EUS|EGD)(?![A-Za-z])'),
    ('结肠镜', r'结肠镜|大肠镜|colonoscop'),
    ('胃镜', r'胃镜|上消化道内镜|gastroscop|esophagogastroduodenoscop|(?<![A-Za-z])EGD(?![A-Za-z])|upper\s+(?:GI|gastrointestinal)\s+endoscop'),
    ('小肠镜', r'小肠镜|enteroscop'),
    ('超声内镜', r'超声内镜|内镜超声|(?<![A-Za-z])EUS(?![A-Za-z])|endoscopic\s+(?:ultrasound|ultrasonograph)'),
    ('CT', r'(?<![A-Za-z])CT(?![A-Za-z])|计算机断层|computed\s+tomograph'),
    ('MRI', r'(?<![A-Za-z])MRI(?![A-Za-z])|(?<![A-Za-z])MR\s+imaging(?![A-Za-z])|磁共振|magnetic\s+resonance'),
    ('超声', r'超声|ultrasound|ultrasonograph|sonograph|(?<![A-Za-z])EUS(?![A-Za-z])'),
    ('X线检查', r'X\s*线|X[- ]ray|radiograph'),
    ('活检', r'活检|(?<![A-Za-z])biops'),
    ('细针穿刺', r'细针(?:穿刺|抽吸)|(?<![A-Za-z])FNA(?![A-Za-z])|fine[- ]needle'),
    ('组织病理学', r'组织学|病理|histolog|patholog'),
    ('免疫组化', r'免疫组化|免疫组织化学|immunohistochem|(?<![A-Za-z])IHC(?![A-Za-z])'),
))
_PROCEDURE_NEGATION = re.compile(
    r'不建议|不应|不能|不可|不必|无需|不需|尚未|未(?:行|做|接受|提供|记录)|没有(?:行|做|提供|记录)|'
    r'\b(?:no|not|without)\s+(?:\w+\s+){0,2}$', re.I)
_PROCEDURE_REJECTION = re.compile(
    r'^[A-Za-z]*(?:\s+(?:evaluation|examination|testing|assessment|procedure|scan|imaging))?\s+'
    r'(?:(?:is|are|was|were|should|would|will|can|could|may|might)\s+)?(?:not|never)\b|'
    r'^(?:检查|评估)?(?:不适用|不推荐|不建议|无需|未实施|未行)', re.I)


def _affirmed_route(text: str, pattern: re.Pattern) -> bool:
    for match in pattern.finditer(text):
        prefix = re.split(r'[，,。；;！？\n]', text[:match.start()])[-1][-18:]
        if not _PROCEDURE_NEGATION.search(prefix) and not _PROCEDURE_REJECTION.search(text[match.end():]):
            return True
    return False


def clinical_procedure_failures(text: str, facts: list[ReportSourceFact],
                                source_quotes: list[str]) -> list[str]:
    """Require a named examination route in this claim's actual sources.

    This is a necessary lexical prerequisite in addition to all semantic and
    applicability checks. A matching word never approves a recommendation.
    Patient facts and this one citation's retained original quotations are the
    only eligible sources; another paper or a generated summary cannot help.
    """
    sources = [value for fact in facts if fact.kind == 'clinical'
        for value in (fact.name, fact.value, fact.source, fact.reference_range) if value]
    sources.extend(quote for quote in source_quotes if isinstance(quote, str))
    missing = [label for label, pattern in _PROCEDURE_ROUTES if _affirmed_route(text, pattern)
        and not any(_affirmed_route(value, pattern) for value in sources)]
    return ['具体检查途径“' + '、'.join(missing) + '”未见于本句绑定的患者原始资料或本篇已选原文；'
        '就医安排同样需要依据。保留已支持的诊断原则与既有专科关注方向，不补写具体检查途径。'] if missing else []


def _explicit_stool(fact: ReportSourceFact) -> bool:
    # A patient's *own* literal specimen label is required. A guideline's
    # eligibility text or another examination on the same day proves nothing.
    for value in (fact.name, fact.source, fact.value):
        for clause in re.split(r'[，,。；;！？\n]', value or ''):
            match = _STOOL.search(clause)
            if match and not _NEGATION.search(clause[max(0, match.start() - 14):match.start()]):
                return True
    return False


def clinical_scope_failures(text: str, facts: list[ReportSourceFact]) -> list[str]:
    """Reject a stool-screening inference from an untyped occult-blood fact.

    This necessary source check supplements independent evidence review. It
    neither changes the positive result nor recommends any procedure itself.
    """
    occult = [fact for fact in facts if fact.kind == 'clinical' and _OCCULT.search(fact.name)]
    if not occult or all(_explicit_stool(fact) for fact in occult):
        return []
    for sentence in re.split(r'[。！？\n]', text):
        # A separate, source-bound imaging referral is not an occult-blood
        # screening claim just because both examinations are in one paragraph.
        if not (_OCCULT.search(sentence) or re.search(r'(?:非结肠镜)?筛查阳性', sentence)):
            if len(facts) != len(occult):
                continue
        for clause in re.split(r'[，,；;]', sentence):
            match = _COLORECTAL.search(clause)
            if not match or _NEGATION.search(clause[:match.start()]):
                continue
            if any(clause.strip() and clause.strip() in fact.value for fact in occult):
                continue  # Literal source instructions remain literal records.
            return ['该隐血来源未明确为粪便检测，不能将其归入粪便或结直肠癌筛查及阳性后结肠镜路径；'
                '保留实际阳性/异常判断与现有随访行动，不以其他标本的指南替代患者前提。']
    return []


def filter_clinical_evidence(record: dict, facts: list[ReportSourceFact]) -> dict | None:
    """Remove stool-paper support assigned to an untyped/urine occult result.

    A generic sentence about false positives does not make a stool-specific
    paper applicable to an unknown test. Other independently scoped clinical
    problems keep their original bindings; no source record is mutated.
    """
    result = deepcopy(record)
    known = {fact.fact_id for fact in facts}
    unknown = {fact.fact_id for fact in facts if fact.kind == 'clinical'
        and _OCCULT.search(fact.name) and not _explicit_stool(fact)}
    if not unknown:
        return result
    primary = record.get('sourceText')
    if not isinstance(primary, str) or not primary.strip():
        primary = next((record[key] for key in ('abstract', 'evidenceText', 'evidence_text')
            if isinstance(record.get(key), str) and record[key].strip()), '')
    title = record.get('title') if isinstance(record.get('title'), str) else ''
    if not _STOOL.search(title + '\n' + primary):
        return result
    problems = result.get('clinicalProblemBindings')
    problems = problems if isinstance(problems, list) else []
    blocked = set(unknown)
    retained = []
    for problem in problems:
        if not isinstance(problem, dict):
            continue
        ids = {source.get('factId') for source in problem.get('sources', []) if isinstance(source, dict)}
        if ids and ids <= unknown:
            if isinstance(problem.get('problemId'), str):
                blocked.add(problem['problemId'])
        elif ids & (known - unknown):
            retained.append(problem)
    bindings = set()
    for key in ('supports', 'microbeIds', 'diseaseIds'):
        if key not in result:
            continue
        values = result[key]
        if not isinstance(values, list):
            continue
        result[key] = [value for value in values if isinstance(value, str) and value not in blocked]
        bindings.update(result[key])
    allowed = (known - unknown) | {problem.get('problemId') for problem in retained
        if isinstance(problem.get('problemId'), str)}
    if not bindings.intersection(allowed) and not retained:
        return None
    if 'clinicalProblemBindings' in result:
        result['clinicalProblemBindings'] = retained
    return result
