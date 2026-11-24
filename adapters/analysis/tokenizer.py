"""AION offline tokenizer adapter for report input budgets."""

from aion.deepseek.tokenizer import Tokenizer


def count_report_input_tokens(text: str) -> int:
    return Tokenizer.count(text)
