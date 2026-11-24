"""Send original DocMind text to the LLM, partitioning only by the input size budget."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from modules.analysis.ports import InputTokenCounter

MAX_INPUT_TOKENS = 102400
MESSAGE_OVERHEAD_TOKENS = 1024
TOKEN_ESTIMATOR = 'aion_deepseek_offline'


@dataclass(frozen=True)
class AnalysisChunk:
    start: int
    end: int
    text: str
    units: tuple[dict, ...] = ()
    records: tuple[dict, ...] = ()
    context: str = ''
    estimated_tokens: int = 0

    @property
    def direct_observations(self):
        return []

    @property
    def evidence_units(self):
        return ()

    @property
    def issues(self):
        return ()


@dataclass
class PreparedInput:
    text: str
    chunks: list[AnalysisChunk]
    structured: bool = False
    source_hash: str = ''
    token_limit: int = MAX_INPUT_TOKENS
    estimated_tokens: int = 0

    def model_text(self, chunk):
        return chunk.text

    @property
    def issues(self):
        return ()


def input_token_budget(
    context_window, output_tokens, system_prompt, correction_prefix, *, count_tokens: InputTokenCounter
):
    if context_window is None:
        return MAX_INPUT_TOKENS
    overhead = count_tokens(system_prompt) + count_tokens(correction_prefix) + MESSAGE_OVERHEAD_TOKENS
    return min(MAX_INPUT_TOKENS, context_window - output_tokens - overhead)


def _fitting_end(content, start, remaining, budget, count_tokens):
    """Find a verified fitting character boundary without decoding token slices."""
    low, high = start + 1, len(content)
    end = start
    # A proportional probe is usually close for long reports with similar rows.
    middle = start + max(1, (high - start) * budget // remaining)
    while low <= high:
        count = count_tokens(content[start:middle])
        if count == budget:
            return middle
        if count < budget:
            end = middle
            low = middle + 1
        else:
            high = middle - 1
        middle = (low + high) // 2
    return end


def _plan_chunks(content, total, limit, count_tokens, *, balanced):
    chunks = []
    start = 0
    remaining = total
    while start < len(content):
        parts = (remaining + limit - 1) // limit
        if parts <= 1:
            end = len(content)
        else:
            # The target leaves sufficient budget for the remaining parts,
            # including when the original count is exactly a multiple of the limit.
            target = (remaining + parts - 1) // parts if balanced else limit
            end = _fitting_end(content, start, remaining, target, count_tokens)
            if not end > start or count_tokens(content[end:]) > (parts - 1) * limit:
                end = _fitting_end(content, start, remaining, limit, count_tokens)
            if end <= start:
                raise ValueError('analysis token budget cannot fit the next character')
            if balanced:
                left = max(start, end - 512)
                for delimiter in ('\n\n', '\n'):
                    boundary = content.rfind(delimiter, left, end)
                    if boundary < 0:
                        continue
                    boundary += len(delimiter)
                    if (
                        count_tokens(content[start:boundary]) <= limit
                        and count_tokens(content[boundary:]) <= (parts - 1) * limit
                    ):
                        end = boundary
                        break
        text = content[start:end]
        estimated_tokens = count_tokens(text)
        if estimated_tokens > limit:
            raise ValueError('analysis token planner produced an oversized part')
        chunks.append(AnalysisChunk(start, end, text, estimated_tokens=estimated_tokens))
        start = end
        remaining = count_tokens(content[start:]) if start < len(content) else 0
    return chunks


def prepare_input(content, *, docmind=None, token_limit=MAX_INPUT_TOKENS, count_tokens: InputTokenCounter):
    # The archived DocMind layout is available for diagnostics, but does not rewrite text.
    if not isinstance(content, str):
        raise TypeError('document content must be text')
    if token_limit < 1:
        raise ValueError('analysis model context leaves no input budget')
    total = count_tokens(content)
    if total <= token_limit:
        chunks = [AnalysisChunk(0, len(content), content, estimated_tokens=total)] if content else []
    else:
        chunks = _plan_chunks(content, total, token_limit, count_tokens, balanced=True)
        if len(chunks) > (total + token_limit - 1) // token_limit:
            # Before accepting an extra part, check the largest fitting cuts
            # without preferring layout boundaries.
            greedy = _plan_chunks(content, total, token_limit, count_tokens, balanced=False)
            if len(greedy) < len(chunks):
                chunks = greedy
    return PreparedInput(
        content,
        chunks,
        source_hash=hashlib.sha256(content.encode('utf-8')).hexdigest(),
        token_limit=token_limit,
        estimated_tokens=total,
    )
