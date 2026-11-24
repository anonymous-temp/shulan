"""Patient-facing Record wording owned by the application."""

import re

from modules.record.application.dialogue import validate_dialogue_focus
from modules.record.application.intake import BACKGROUND_ACCEPT
from modules.record.application.intake import BACKGROUND_DECLINE
from modules.record.application.intake import DIMENSION_LABELS
from modules.record.application.intake import LIFESTYLE_DIMENSIONS
from modules.record.application.intake import _fragments
from modules.record.application.lifestyle import bridge_text
from modules.record.application.public_text import validate_internal_notice
from modules.record.application.validation import RecordSourceError
from modules.record.application.validation import RecordValidationError

ANSWER_HINT = '也可以通过文字或语音补充描述。'
NO_MORE_OPTION = '没有了'
SUPPLEMENT_QUESTION = '您还有其他想了解的问题吗？可以通过文字或语音补充描述'
CLARIFY_SUPPLEMENT_QUESTION = '请通过文字或语音告诉我具体问题。'
_LEGACY_SUPPLEMENT_QUESTIONS = (
    '您还有其他想了解的问题吗？可以直接通过文字或语音告诉我，也可以选择“没有了”。',
    '请直接说说您想了解的具体问题；如果没有其他问题，可以选择“没有了”。',
    '您还有其他想了解的问题吗？如果没有，可以选择“没有了”。 ' + ANSWER_HINT,
)
COMPLETION_MESSAGE = '好的，本次建档已结束，感谢您的配合。'
_OPTION_INSTRUCTION = re.compile(
    r'\s*(?:[（(]\s*请(?:补充|说明|描述|填写)(?:说明|描述|情况|内容)?\s*[）)]'
    r'|[，,：:]\s*请(?:补充|说明|描述|填写)(?:说明|描述|情况|内容)?)\s*$'
)
_OLD_ANSWER_HINT = re.compile(
    r'[，,。\s]*(?:也可以|也可|也欢迎|欢迎)(?:您)?(?:直接)?(?:补充描述|补充说明|补充)[。！!\s]*$'
)
_FREE_ANSWER_HINT = re.compile(
    r'[，,。；;\s]*(?:也可以|也可|可以|也欢迎|欢迎|请)?(?:您)?(?:直接)?(?:通过|使用|用)?'
    r'文字(?:或|、|和|/|／)语音(?:来)?(?:补充描述|补充说明|补充回答|补充|描述|回答|输入|告诉我)'
    r'(?:您的回答|您的情况|具体情况|具体内容)?[。！!\s]*$'
)
_AMBIGUOUS_CHOICE = re.compile(r'其[他它]|\bothers?\b', re.IGNORECASE)
_ENTRY_ONLY_CHOICES = frozenset(
    (
        '有补充',
        '还有补充',
        '需要补充',
        '有需要补充的内容',
        '有日常习惯想补充',
        '补充内容',
        '补充说明',
        '需要更正',
        '点击补充',
        '点击填写',
        '自由填写',
        '自行填写',
        '自行输入',
        '自定义',
    )
)
_QUOTE_OPEN = r'[“「『‘\"\']'
_QUOTE_CLOSE = r'[”」』’\"\']'
_UI_TARGET = r'(?:其[他它][^，,。.!！?？;；“”「」『』‘’\"\']{0,12}|(?:某|该|这个|所选|相应)?(?:快捷)?(?:选项|按钮))'
_INPUT_ACTION = r'(?:输入|填写|填入|补充|说明|描述)'
_TEXT_ENTRY_ACTION = r'(?:输入|填写|填入|补充(?=说明|描述|内容|信息|情况|病情|病名|答案|具体|[，,。.!！?？;；]|$))'
_UI_ENTRY_PATTERNS = (
    re.compile(
        rf'(?:在|到|从|通过){_QUOTE_OPEN}?(?:某|该|这个|所选|相应)?(?:快捷)?(?:选项|按钮){_QUOTE_CLOSE}?(?:里|中|内|处|下)?(?:直接|继续|自行|再|进行)?{_INPUT_ACTION}'
    ),
    re.compile(
        rf'(?:在|到|从|通过){_QUOTE_OPEN}?{_UI_TARGET}{_QUOTE_CLOSE}?(?:选项|按钮)?(?:里|中|内|处|下)?(?:直接|继续|自行|再|进行)?{_TEXT_ENTRY_ACTION}'
    ),
    re.compile(
        rf'(?:在|到){_QUOTE_OPEN}[^，,。.!！?？;；“”「」『』‘’\"\']{{1,24}}{_QUOTE_CLOSE}(?:选项|按钮)(?:里|中|内|处)?(?:直接|继续|自行|再|进行)?{_INPUT_ACTION}'
    ),
    re.compile(
        rf'(?:点击|点选|勾选|选择|选中)(?:{_QUOTE_OPEN}[^，,。.!！?？;；“”「」『』‘’\"\']{{1,24}}{_QUOTE_CLOSE}|{_UI_TARGET})(?:选项|按钮)?(?:后|之后|以后)(?:请|可|可以|再|继续|直接|即可|就能)*{_TEXT_ENTRY_ACTION}'
    ),
    re.compile(
        r'(?:点击|点选|勾选|选择|选中)[^，,。.!！?？;；]{0,30}(?:弹出|打开|出现|展开)[^，,。.!！?？;；]{0,10}(?:输入框|文本框|填写框)'
    ),
)
_NEGATED_ENTRY = re.compile(r'(?:不能|不要|不可|不应|无法|不支持|并非|请勿|不会)(?:直接|继续|再|自行|自动|尝试)?$')
_ACKNOWLEDGEMENT_ECHO = re.compile(
    r'(?:您|你)(?:(?:这次|本次|刚才|刚刚|已经|已))?'
    r'(?:选择|选了|选的是|勾选|点击|回答(?:的)?是|提到|说到|说了|说过|表示|已说明|已经说明|想(?:要|了解|知道|改善)|希望(?:了解|知道|改善))'
)
_ACKNOWLEDGEMENT_RECEIPT = re.compile(r'已(?:经)?(?:记录|了解|记下)')
_INTAKE_METHOD_EXPLANATION = re.compile(
    r'(?:这些|这类|上述)(?:信息|情况|内容|资料)[^。！？!?]{0,35}'
    r'(?:有助于|能帮助|帮助我们|便于|用于|能够|能让)'
    r'|(?:了解|询问|收集|记录|补充)[^。！？!?]{0,40}'
    r'(?:能让后续|有助于(?:判断|分析|了解|评估)|能帮助我们|提供(?:依据|背景)|避免把|避免将)'
    r'|(?:避免把|避免将)[^。！？!?]{0,35}(?:误当|当作|误判|确诊)'
    r'|(?:能|能够|可以)帮助(?:我们|我)[^。！？!?]{0,20}(?:了解|判断|分析)'
    r'|(?:有助于|便于)(?:我们|我)(?:一起)?(?:理清重点|明确重点|梳理信息)'
)
_QA_INFORMATION_REQUEST = re.compile(
    r'(?:请|麻烦)(?:您)?(?:先|再|进一步|详细|具体)?(?:告诉我|告知我|说明一下|描述一下)'
    r'|(?:请|需要)(?:您)?(?:先|再|进一步|详细)?(?:补充|提供|告知|说明)'
    r'(?:一下|完整的|具体的|更多|有关|相关)?(?:您|你)?(?:的)?(?:个人|家族|具体)?'
    r'(?:信息|资料|报告|检查结果|情况|亲属|年龄|疾病史|病史|症状|发病年龄|是哪位|哪位)'
    r'|(?:能否|可否|能不能|可不可以)(?:请)?(?:您)?(?:告诉|告知|补充|提供|说明)'
    r'|(?:请问|方便说说|方便告诉)(?:您|你|具体|是|一下)'
    r'|(?:您|你)[^。！？!?]{0,100}[？?]'
)
_QA_INFORMATION_LIMIT = re.compile(
    r'(?:目前|现有|当前|已有|这些|已知|有限)(?:的)?(?:信息|资料|情况|描述|内容)'
    r'[^。！？!?；;\n]{0,48}(?:无法|不能|难以|很难|没法|不足以|不便|不宜)'
    r'[^。！？!?；;\n]{0,20}(?:判断|确定|明确|得知|回答|回复|评估|诊断)'
    r'|(?:信息|资料)(?:还|尚|仍)?(?:不足|不充分|不完整|不全|不够)'
    r'|(?:无法|不能|难以|很难|没法)[^。！？!?；;\n]{0,16}'
    r'(?:根据|基于|凭|从)(?:目前|现有|当前|已有|这些|已知)(?:的)?(?:信息|资料|情况|描述)'
    r'[^。！？!?；;\n]{0,16}(?:判断|确定|明确|得知|回答|回复|评估|诊断)'
    r'|(?:缺少|缺乏|未提供|没有提供|尚未提供|尚未说明|未说明)'
    r'[^。！？!?；;\n]{0,28}(?:信息|资料|症状|检查|报告|具体情况)'
    r'[^。！？!?；;\n]{0,48}(?:无法|不能|难以|很难|没法)'
    r'[^。！？!?；;\n]{0,20}(?:判断|确定|明确|得知|回答|回复|评估|诊断)'
)
_BACKGROUND_PREFIX = re.compile(
    r'^(?:(?:从|根据)(?:您|你)(?:所)?(?:提供|描述|给出|说明)(?:的)?(?:信息|情况|内容|资料|描述)(?:来)?看?[，,:：]?'
    r'|(?:从|根据)(?:您|你)(?:的)?(?:描述|回答|情况|信息)(?:来)?看?[，,:：]?'
    r'|(?:您|你)(?:的)?(?:情况是|目前的情况是)[，,:：]?'
    r'|家族中|您家中|您的家族中)'
)
_BACKGROUND_PERSON = (
    r'(?:您本人|你本人|您|你|(?:您的?|你的?)?'
    r'(?:母亲|父亲|外祖父|外祖母|祖父|祖母|爷爷|奶奶|外公|外婆|父母|兄弟姐妹|血亲)'
    r'(?:[、和及与](?:母亲|父亲|外祖父|外祖母|祖父|祖母|爷爷|奶奶|外公|外婆|兄弟姐妹|血亲))*)'
)
_BACKGROUND_FACT = re.compile(
    rf'^{_BACKGROUND_PERSON}(?:也|都|均|已|已经|曾|曾经|明确)?'
    r'(?:确诊(?:了|为)?|被确诊(?:了|为)?|被诊断为|患有|有|患)'
    r'[^，,。！？!?；;\n]{1,40}$'
)
_EXPLANATION_LINK = re.compile(
    r'如果|若|时|提示|意味着|说明|相关|不等于|并不|可能|通常|一般|有助于|有利于|值得|需要|应当|建议'
)
_RECAP_MOTIVATION = re.compile(
    r'(?:确实|自然|因此|所以)?(?:会|容易|难免)?(?:关心|担心|关注|想了解)(?:遗传|风险|背景|影响)'
)
_COPY_PREFIX = re.compile(r'^(?:从您提供的信息看|根据您的描述|从您的描述来看|家族中|您家中|您的家族中)[，,：:]?')
_COPY_PUNCTUATION = re.compile(r'[\W_]+', re.UNICODE)


def _validate_information_limit(text: str) -> None:
    if _QA_INFORMATION_LIMIT.search(text):
        raise RecordValidationError(
            'qa_information_deferral_forbidden',
            '根据已有信息直接解释；不要在任何句子中强调信息不足、仅凭现有资料无法判断，即使后面还有建议',
        )


def _validate_background_recap(text: str) -> None:
    """Reject an explicit patient-history preamble, not conditional explanation."""
    first = re.split(r'[。！？!?；;\n]', text.strip(), maxsplit=1)[0]
    if not first or _EXPLANATION_LINK.match(first):
        return
    stripped = _BACKGROUND_PREFIX.sub('', first)
    clauses = [clause.strip() for clause in re.split(r'[，,]', stripped) if clause.strip()]
    if not clauses:
        return
    initial = clauses[0]
    # A factual condition belongs inside an explanation. Do not ban a disease
    # keyword, a general family-health statement or an actual medical boundary.
    if not _BACKGROUND_FACT.fullmatch(initial) or _EXPLANATION_LINK.search(initial):
        return
    personal = initial.startswith(('您', '你'))
    specific_relatives = len(re.findall(r'母亲|父亲|外祖父|外祖母|祖父|祖母|爷爷|奶奶|外公|外婆', initial)) >= 2
    second_fact = any(_BACKGROUND_FACT.fullmatch(clause) for clause in clauses[1:])
    if stripped != first or personal or specific_relatives or second_fact or _RECAP_MOTIVATION.search(first):
        raise RecordValidationError(
            'background_recap_forbidden',
            '不要以亲属、本人病史清单或代述担忧开头；直接解释问题，必要背景只作为解释中的条件',
        )


def _copy_normalized(text: str) -> str:
    return _COPY_PUNCTUATION.sub('', _COPY_PREFIX.sub('', text.strip())).casefold()


def _copy_units(text: str) -> list[str]:
    units = []
    for sentence in re.split(r'[。！？!?；;\n]+', text):
        if value := _copy_normalized(sentence):
            units.append(value)
        for clause in re.split(r'[，,]+', sentence):
            if value := _copy_normalized(clause):
                units.append(value)
    return list(dict.fromkeys(units))


def _shared_long_copy(first: str, second: str) -> bool:
    left, right = _copy_normalized(first), _copy_normalized(second)
    if left and left == right and len(left) >= 6:
        return True
    for left in _copy_units(first):
        for right in _copy_units(second):
            shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
            if len(shorter) >= 20 and shorter in longer:
                return True
    return False


def _validate_internal_copy(text: str) -> None:
    sentences = [value.strip() for value in re.split(r'[。！？!?；;\n]+', text) if value.strip()]
    for index, sentence in enumerate(sentences):
        if any(_shared_long_copy(sentence, previous) for previous in sentences[:index]):
            raise RecordValidationError('duplicate_response_copy', '同一答复不要重复相同长句段，保留一次解释即可')
        clauses = [_copy_normalized(value) for value in re.split(r'[，,]+', sentence) if value.strip()]
        if any(len(value) >= 20 and value in clauses[:index] for index, value in enumerate(clauses)):
            raise RecordValidationError('duplicate_response_copy', '同一答复不要重复相同长句段，保留一次解释即可')


def validate_acknowledgement_style(message, *, action: str, has_responses: bool = False) -> None:
    """Check newly written bridging text, never source quotes or cached turns."""
    for text in (message.acknowledgement, message.rationale):
        validate_internal_notice(text)
    if action == 'finish':
        return
    if not has_responses and not message.acknowledgement.strip():
        raise RecordValidationError('acknowledgement_required', '没有正式答复时仍需简短自然的承接，不能省略')
    if action in ('open_intake', 'ask_intake') and _INTAKE_METHOD_EXPLANATION.search(message.acknowledgement):
        raise RecordValidationError(
            'intake_method_explanation_forbidden',
            '用一至两句面向用户的自然交流连接当前问题，可表达帮助用户的意图，不解释采集方法、信息用途或避免误判的作用',
        )
    for text in (message.acknowledgement, message.rationale):
        _validate_information_limit(text)
        _validate_background_recap(text)
        if _ACKNOWLEDGEMENT_ECHO.search(text) or _ACKNOWLEDGEMENT_RECEIPT.search(text) or re.search(r'[？?]', text):
            raise RecordValidationError(
                'acknowledgement_style_invalid',
                '承接用一至两句自然交流，可简短引用真实关注点或延续前文，'
                '不复述选择或整段背景，不作已记录回执；提问只放question',
            )


def validate_response_style(text: str, *, previous_response: str = '') -> None:
    """QA explains at the available precision, without deferral or repetition."""
    validate_internal_notice(text)
    _validate_information_limit(text)
    _validate_background_recap(text)
    if _QA_INFORMATION_REQUEST.search(text):
        raise RecordValidationError(
            'qa_information_request_forbidden',
            '直接给一般解释、具体生活场景及可执行建议，不要求用户补齐资料，也不以信息不足代替回答；不编造个体诊断',
        )
    _validate_internal_copy(text)
    if previous_response and _shared_long_copy(text, previous_response):
        raise RecordValidationError(
            'repeated_previous_response',
            '用户主动重问时简短补充或换一种解释，不照搬已经展示过的答复或相同长句段',
        )


def validate_copy_components(message, responses, *, action: str) -> None:
    """Check model components before publishing their combined copy."""
    texts = [response if isinstance(response, str) else response.text for response in responses]
    for index, text in enumerate(texts):
        try:
            validate_internal_notice(text)
            _validate_internal_copy(text)
            if any(_shared_long_copy(text, previous) for previous in texts[:index]):
                raise RecordValidationError(
                    'duplicate_response_copy', '不同疑问的答复不要重复相同长句段，分别回应各自重点'
                )
        except RecordValidationError as error:
            error.diagnostic_fields = {'component': 'responses', 'responseIndex': index}
            raise
    if action != 'finish' and not (texts and action == 'invite_supplement'):
        bridge = message.acknowledgement
        if any(_shared_long_copy(bridge, text) for text in texts):
            error = RecordValidationError(
                'acknowledgement_repeats_answer',
                '正式答复已自然承接时不要再复述背景或重复正文；保留有效答复，移除重复承接',
            )
            error.diagnostic_fields = {'component': 'assistantMessage'}
            raise error


def choice_context_options(*groups) -> list[str] | None:
    """Both proposed and final labels matter when rejecting button-input claims."""
    options = list(dict.fromkeys(option for group in groups if isinstance(group, list) for option in group))
    return options or None


_CHOICE_NUMBER = r'(?:\d+(?:\.\d+)?|[零一二两三四五六七八九十半]+)'
_CHOICE_UNIT = (
    r'(?:毫升|公升|升|毫克|微克|千克|公斤|克|斤|公里|千米|厘米|米|分钟|小时|秒|天|日|周|月|年'
    r'|次|杯|瓶|支|包|份|拳|片|粒|%|[a-zA-Zµμ°]+)'
)
_CHOICE_FREQUENCY = rf'(?:(?:每|隔)(?:{_CHOICE_NUMBER})?(?:小时|天|日|周|月|年|晚))?'
_CHOICE_QUANTITY = (
    rf'{_CHOICE_FREQUENCY}(?:约|不足|少于|超过)?{_CHOICE_NUMBER}'
    rf'(?:(?:至|～|~|－|-)(?:不足)?{_CHOICE_NUMBER})?{_CHOICE_UNIT}(?:[／/]{_CHOICE_UNIT})*'
)


def _combines_choice_answers(label: str) -> bool:
    """List separators cannot hide several answers; quantity syntax is distinct."""
    if not re.search(r'或|、|[／/]', label):
        return False
    # A range tail describes the same quantity, not a second behavior or disease.
    if re.fullmatch(_CHOICE_QUANTITY + r'或(?:更少|更短|更低|更早|更晚|更多|更长|更高|以上|以下|以内)', label):
        return False
    if re.fullmatch(_CHOICE_QUANTITY, label):
        return False
    # A fractional amount still has one unit and one meaning.
    if re.fullmatch(rf'{_CHOICE_FREQUENCY}{_CHOICE_NUMBER}[／/]{_CHOICE_NUMBER}{_CHOICE_UNIT}', label):
        return False
    return True


def validate_choice_labels(options: list[str]) -> None:
    """New shortcuts name one answer; explanations belong in the question."""
    broad_labels = {
        '身体不适或症状',
        '身体不适',
        '健康问题',
        '健康管理',
        '慢性病管理',
        '报告解读',
        '检查结果的意义',
        '最近的身体变化',
        '日常生活如何调整',
        '整体健康情况',
        '日常体力与精力',
        '饮食与体重管理',
        '长期健康维护',
        '睡眠质量',
    }
    for option in options:
        compact = re.sub(r'\s+', '', option)
        if (
            _AMBIGUOUS_CHOICE.search(compact)
            or compact in _ENTRY_ONLY_CHOICES | broad_labels
            or re.search(r'某|[（(\[【]|[）)\]】]', compact)
            or _combines_choice_answers(compact)
            or len(compact) > 24
        ):
            raise RecordValidationError(
                'ambiguous_shortcut_forbidden',
                '快捷选项须简短、独立且明确；不使用泛化占位、括号说明或拼接不同答案，选项外内容通过文字或语音回答',
            )


def validate_answer_entry_instructions(text: str, *, options: list[str] | None = None) -> None:
    """Guard published components before checking button-input instructions."""
    validate_internal_notice(text)
    compact = re.sub(r'\s+', '', text)
    patterns = list(_UI_ENTRY_PATTERNS)
    if options:
        labels = '|'.join(re.escape(re.sub(r'\s+', '', label)) for label in options if label)
        if labels:
            patterns.append(
                re.compile(
                    rf'(?:在|到){_QUOTE_OPEN}?(?:{labels}){_QUOTE_CLOSE}?(?:(?:选项|按钮)(?:里|中|内|处)?|里|中|内|处)(?:直接|继续|再)?{_TEXT_ENTRY_ACTION}'
                )
            )
    for pattern in patterns:
        for match in pattern.finditer(compact):
            if _NEGATED_ENTRY.search(compact[max(0, match.start() - 8) : match.start()]):
                continue
            if pattern is _UI_ENTRY_PATTERNS[-1]:
                popup = re.search(r'弹出|打开|出现|展开', match.group())
                if popup and _NEGATED_ENTRY.search(match.group()[: popup.start()]):
                    continue
            raise RecordValidationError(
                'invalid_answer_entry_instruction',
                '选项按钮只能选择，不能填写或弹出输入框；补充内容应通过现有文字或语音入口回答',
            )


def _whitespace(value: str) -> str:
    # Preserve every non-whitespace character, including units and comparators.
    return re.sub(r'\s+', ' ', value).strip()


def _scalar(value) -> str:
    return _whitespace(str(value)) if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ''


def report_context_lines(report: dict, index: int) -> tuple[list[str], list[str]]:
    """Render confirmed fields into complete, directly quotable lines."""
    header = [f'报告 {index}']
    for label, key in (
        ('标题', 'title'),
        ('报告时间', 'reportTime'),
        ('报告类型', 'reportType'),
        ('状态', 'status'),
        ('解读摘要', 'aiSummary'),
    ):
        if value := _scalar(report.get(key)):
            header.append(f'{label}：{value}')
    rows = []
    seen = set()
    sections = [section for section in report.get('sections') or [] if isinstance(section, dict)]
    section_by_index = {section.get('sectionIndex', index): section for index, section in enumerate(sections)}

    def add(line, flag=None):
        if line and line not in seen:
            seen.add(line)
            rows.append((flag not in (None, '', '正常', 'normal'), line))

    def visit(node):
        expansion = node.get('expansion')
        observations = node.get('observations')
        # Historical complete snapshots can have complementary lists. New
        # snapshots mirror them, and add() removes only identical rendered facts.
        items = (observations if isinstance(observations, list) else []) + (
            expansion if isinstance(expansion, list) else []
        )
        for item in items:
            if not isinstance(item, dict):
                continue
            name, value = _scalar(item.get('itemName')), _scalar(item.get('resultValue'))
            if not name or not value:
                continue
            unit = _scalar(item.get('unit'))
            if unit and not value.endswith(unit):
                value += ' ' + unit
            fields = [f'{name}：{value}']
            for label, key in (
                ('参考范围', 'referenceRange'),
                ('标记', 'flag'),
                ('标本', 'specimen'),
                ('方法', 'method'),
            ):
                if extra := _scalar(item.get(key)):
                    fields.append(f'{label}：{extra}')
            if kind := _scalar(item.get('kind')):
                fields.append('结果类型：' + {'measurement': '测量', 'assessment': '评估'}.get(kind, kind))
            owner = section_by_index.get(item['sourceSection'], {}) if 'sourceSection' in item else node
            observed = item.get('observedAt') or owner.get('reportTime')
            if observed := _scalar(observed):
                fields.append('日期：' + observed)
            if owner is not report and (title := _scalar(owner.get('title'))):
                fields.append('检查：' + title)
            add('；'.join(fields), item.get('flag'))
        if isinstance(expansion, dict):
            for label, key in (
                ('影像所见', 'findings'),
                ('影像诊断', 'diagnosis'),
                ('体检机构', 'checkupOrgName'),
                ('体检时间', 'checkupTime'),
            ):
                if value := _scalar(expansion.get(key)):
                    line = f'{label}：{value}'
                    if node.get('reportTime') and key in ('findings', 'diagnosis'):
                        line += '；日期：' + _scalar(node['reportTime'])
                    if node is not report and (title := _scalar(node.get('title'))):
                        line += '；检查：' + title
                    add(line)

    visit(report)
    for section in sections:
        visit(section)
    return header, [line for _, line in sorted(rows, key=lambda row: not row[0])]


def bounded_report_context(report: dict, index: int, limit: int) -> str:
    header, facts = report_context_lines(report, index)
    lines = header + facts
    if len('\n'.join(lines)) <= limit:
        return '\n'.join(lines)
    marker = '（仅展示可完整引用的部分资料，完整结果已保存）'
    retained = []
    remaining = max(0, limit - len(marker) - 1)
    for line in lines:
        cost = len(line) + (1 if retained else 0)
        if cost <= remaining:
            retained.append(line)
            remaining -= cost
    return '\n'.join([*retained, marker]) if len(marker) <= limit else ''


def normalize_option(value: str) -> str:
    """Remove known UI instructions, never reinterpret an answer's meaning."""
    value = _OPTION_INSTRUCTION.sub('', value.strip()).strip()
    if '请补充' in value:
        raise RecordValidationError('option_contains_instruction', '选项不能包含操作提示，请将补充说明放在问题末尾')
    return value


def with_answer_hint(question: str) -> str:
    if question in (SUPPLEMENT_QUESTION, CLARIFY_SUPPLEMENT_QUESTION):
        return question
    if question.endswith(ANSWER_HINT):
        return question
    question = _FREE_ANSWER_HINT.sub('', question).rstrip()
    question = _OLD_ANSWER_HINT.sub('', question).rstrip()
    return question + (' ' if question.endswith(('。', '？', '?', '！', '!')) else '。') + ANSWER_HINT


def supplement_question(*, clarify: bool = False) -> dict:
    return {
        'question': CLARIFY_SUPPLEMENT_QUESTION if clarify else SUPPLEMENT_QUESTION,
        'questionType': 'radio',
        'options': [NO_MORE_OPTION],
    }


def is_supplement_question(value: dict) -> bool:
    """Recognize the published fixed invitation, including its answer hint."""
    if value.get('questionType') != 'radio' or value.get('options') != [NO_MORE_OPTION]:
        return False
    accepted = (SUPPLEMENT_QUESTION, CLARIFY_SUPPLEMENT_QUESTION, *_LEGACY_SUPPLEMENT_QUESTIONS)
    return value.get('question') in accepted or any(
        value.get('question') == with_answer_hint(question) for question in _LEGACY_SUPPLEMENT_QUESTIONS
    )


def question_task(action: str, target: dict | None = None) -> dict:
    """Resolve deterministic questions before the presentation model is called."""
    fixed = action in ('invite_supplement', 'clarify_supplement')
    if action == 'invite_background':
        label = DIMENSION_LABELS.get((target or {}).get('dimension'), '日常生活')
        return {
            'questionMode': 'fixed',
            'fixedQuestion': {
                'question': f'为了补充报告之外的健康背景，您愿意再聊聊{label}方面的情况吗？',
                'questionType': 'radio',
                'options': [BACKGROUND_ACCEPT, BACKGROUND_DECLINE],
            },
        }
    return {
        'questionMode': 'fixed' if fixed else 'none' if action == 'finish' else 'generated',
        'fixedQuestion': supplement_question(clarify=action == 'clarify_supplement') if fixed else None,
    }


def acknowledgement_text(message, *, action: str) -> str:
    """Publish a single conversational introduction, not the selection rationale."""
    if action == 'finish':
        return COMPLETION_MESSAGE
    return message.acknowledgement


def default_acknowledgement(flow: dict) -> str:
    """A source-neutral bridge cannot invent an individual's health status."""
    dimension = (flow.get('focus') or {}).get('dimension')
    if dimension == 'medical_history':
        return '先聊聊您以往的疾病情况。'
    if dimension == 'family_history':
        return '接着聊聊家里人的健康情况。'
    if dimension in LIFESTYLE_DIMENSIONS:
        return bridge_text(dimension)
    return (
        '接下来可以聊聊您关心的问题。'
        if flow.get('phase') in ('qa', 'supplement')
        else '您好，我们从您这次最想解决的问题聊起。'
    )


def validate_message(message, patient: dict, *, action: str, has_responses: bool = False) -> None:
    """Check source availability, not verbatim wording or semantic equivalence."""
    validate_acknowledgement_style(message, action=action, has_responses=has_responses)
    validate_dialogue_focus(message, patient, action=action)
    validate_answer_entry_instructions(message.acknowledgement)
    validate_answer_entry_instructions(message.rationale)
    sources = {
        '本次回答': list(_fragments(patient['本次回答'])),
        '历史回答': [text for item in patient['历史问答'] for text in _fragments(item['用户回答'])],
        '本次报告': list(_fragments(patient['本次报告'])),
        '长期画像': list(_fragments(patient['长期健康画像'])),
        '附带资料': list(_fragments(patient['附带健康资料'])),
        '基本信息': list(_fragments(patient.get('基本信息'))),
    }
    for index, evidence in enumerate(message.evidence):
        # Internal evidence may summarize several passages. A string match does
        # not establish factual correctness and must not block the whole turn.
        if not any(fragment.strip() for fragment in sources[evidence.source]):
            raise RecordSourceError(index=index, source=evidence.source)
    # Intake introductions may link to a historical goal/report, or simply
    # change topic. Requiring the latest answer as evidence encourages a recap
    # even when it has no bearing on the selected question.
    if patient['本次回答'] is not None and action not in ('finish', 'open_intake', 'ask_intake'):
        if not any(e.source == '本次回答' for e in message.evidence):
            raise RecordValidationError(
                'current_answer_evidence_required', '承接说明必须联系本次回答，不能只复述报告或泛泛表示已了解'
            )
