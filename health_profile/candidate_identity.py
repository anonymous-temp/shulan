"""同一来源内多值画像候选的稳定身份分配。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .models import MemoryCandidate


@dataclass(frozen=True, slots=True)
class PreparedCandidateIdentity:
    candidate: MemoryCandidate
    logical_slot: tuple[str, ...]
    value_fingerprint: str
    legacy_candidate_id: str


@dataclass(frozen=True, slots=True)
class CandidateIdentityResult:
    candidates: tuple[MemoryCandidate, ...]
    deduplicated_count: int


def finalize_candidate_identities(
        prepared: list[PreparedCandidateIdentity],
) -> CandidateIdentityResult:
    """去除完全相同的候选，并为同槽多值候选保留稳定 ID。

    单值槽继续使用旧 candidateId，保证已完成任务重放兼容；只有同一
    来源槽确实包含多个不同值时，才使用调用方预先构造的多值 ID。
    """
    unique = []
    seen_values = set()
    deduplicated = 0
    for item in prepared:
        value_slot = (item.logical_slot, item.value_fingerprint)
        if value_slot in seen_values:
            deduplicated += 1
            continue
        seen_values.add(value_slot)
        unique.append(item)

    slot_counts = Counter(item.logical_slot for item in unique)
    candidates = tuple(
        (
            item.candidate.model_copy(update={
                'candidate_id': item.legacy_candidate_id,
            })
            if slot_counts[item.logical_slot] == 1
            else item.candidate
        )
        for item in unique
    )
    return CandidateIdentityResult(
        candidates=candidates,
        deduplicated_count=deduplicated,
    )
