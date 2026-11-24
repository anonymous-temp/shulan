"""Map an explicit source enterotype to the agreed three-type display rule."""
import re
import unicodedata


_TYPES = (
    ('I型', '拟杆菌型', ('拟杆菌型', '拟杆菌肠型', '拟杆菌属型', 'Bacteroides',
        'Bacteroides type', 'Bacteroides enterotype', 'I型', '肠型I')),
    ('II型', '普氏菌型', ('普氏菌型', '普氏菌肠型', '普雷沃氏菌型', '普雷沃氏菌肠型',
        'Prevotella', 'Prevotella type', 'Prevotella enterotype', 'II型', '肠型II')),
    ('III型', '瘤胃球菌型', ('瘤胃球菌型', '瘤胃球菌肠型', 'Ruminococcus',
        'Ruminococcus type', 'Ruminococcus enterotype', 'III型', '肠型III')),
)


def _key(value):
    return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', value)).casefold()


def classify_explicit_enterotypes(rows):
    """Only an explicit sourced result supplies a type; abundances never do.

    The classification is a report rule, not an extra native grade or disease
    judgement. Literal value, source label and provenance remain untouched.
    """
    for item in rows:
        if item.code != 'enterotype' or not item.sources or not isinstance(item.value, str):
            continue
        key = _key(item.value)
        matched = [(level, label) for level, label, aliases in _TYPES
            if key in {_key(alias) for alias in aliases}]
        if len(matched) != 1:
            continue
        level, label = matched[0]
        item.assessment_level = level
        item._assessment_origin = 'report'
        item.explanation = f'{level}（{label}）。'
