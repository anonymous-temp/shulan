"""Pure text checks that callers can apply before publishing generated output.

These checks detect encoding artifacts, not semantic correctness. Script checks
are opt-in: legitimate foreign terms copied from source material are preserved.
No text is repaired by deleting characters, and errors contain no patient text.
"""

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class TextIssue:
    path: str
    code: str
    offset: int


class TextQualityError(ValueError):
    def __init__(self, issue: TextIssue):
        self.issue = issue
        super().__init__(f'generated_text:{issue.code}:{issue.path}:{issue.offset}')


_WORDS = re.compile(r'[^\W_]+', re.UNICODE)
_MOJIBAKE = re.compile(r'锟斤拷|烫{3,}|ï¿½')
_HIDDEN = frozenset('\u200b\ufeff\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069')


def _han(character: str) -> bool:
    return unicodedata.name(character, '').startswith(('CJK UNIFIED IDEOGRAPH', 'CJK COMPATIBILITY IDEOGRAPH'))


def _unexpected_letter(character: str) -> bool:
    return unicodedata.category(character).startswith('L') and not (
        _han(character) or unicodedata.name(character, '').startswith(('LATIN ', 'GREEK '))
    )


def _sourced_foreign_runs(word: str, source_text: str) -> bool:
    # A patient name/term can acquire a Chinese prefix when acknowledged. Match
    # its foreign spelling, not the newly composed surrounding Chinese sentence.
    runs = ''.join(char if _unexpected_letter(char) else ' ' for char in word).split()
    return bool(runs) and all(run in source_text for run in runs)


def check_text(text: str, *, path: str = '$', chinese_context: bool = False, source_text: str = '') -> None:
    """Raise on a high-confidence defect; allow units, Greek letters and emoji."""
    for offset, character in enumerate(text):
        point = ord(character)
        code = None
        if character == '\ufffd':
            code = 'replacement_character'
        elif 0xD800 <= point <= 0xDFFF:
            code = 'unpaired_surrogate'
        elif unicodedata.category(character) == 'Cc' and character not in '\t\n\r':
            code = 'control_character'
        elif character in _HIDDEN or point & 0xFFFF in (0xFFFE, 0xFFFF) or 0xFDD0 <= point <= 0xFDEF:
            code = 'invisible_or_noncharacter'
        if code:
            raise TextQualityError(TextIssue(path, code, offset))
    for match in _MOJIBAKE.finditer(text):
        if match.group() not in source_text:
            raise TextQualityError(TextIssue(path, 'mojibake_marker', match.start()))
    if chinese_context:
        for match in _WORDS.finditer(text):
            word = match.group()
            if any(_han(char) for char in word) and any(_unexpected_letter(char) for char in word):
                if word not in source_text and not _sourced_foreign_runs(word, source_text):
                    raise TextQualityError(TextIssue(path, 'mixed_script_word', match.start()))


def check_generated_value(value, *, path: str = '$', chinese_context: bool = False, source_text: str = '') -> None:
    """Check complete parsed JSON, including strings inside arrays and objects."""
    if isinstance(value, str):
        check_text(value, path=path, chinese_context=chinese_context, source_text=source_text)
    elif isinstance(value, dict):
        for index, (key, item) in enumerate(value.items()):
            check_text(str(key), path=f'{path}.key[{index}]', chinese_context=chinese_context, source_text=source_text)
            # Paths use positions, so a model-generated key cannot leak into logs.
            check_generated_value(
                item, path=f'{path}.value[{index}]', chinese_context=chinese_context, source_text=source_text
            )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            check_generated_value(
                item, path=f'{path}[{index}]', chinese_context=chinese_context, source_text=source_text
            )
