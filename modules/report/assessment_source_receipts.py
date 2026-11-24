"""Deterministic bookkeeping around the independent source review.

The independent reviewer reads frozen paragraphs (P) and returns a receipt: one
entry per P, one decision per server-listed claim (K) and its violations. Three
kinds of deterministic help live here, none of which relaxes a source check:

* Receipt normalization. A K returned under a sibling P or twice, a violation
  naming records or passages that its P never used, citation ids attached to a
  use that takes none, and a duplicated P entry are bookkeeping, not content.
  They are corrected before validation instead of costing a review round. No
  verdict is weakened and no violation is dropped.
* Server repairs of receipt-only findings. A management action written inside
  an interpretation paragraph is moved to its own action paragraph without the
  background citations; an action paragraph sheds citations the reviewer did
  not attach to any action; a recalled specimen event gets the fixed sentence
  saying its timing relation is unconfirmed. Repaired text is validated again.
* Sentence retention. When a duty is still unresolved after the last repair,
  only the clauses that the review actually rejected are removed. What remains
  must still pass the deterministic checks, or the duty falls back to a
  source-only listing of its recorded values.
"""

from __future__ import annotations

import re
import unicodedata
from copy import deepcopy

RECALL_TIMING_NOTE = '上述回顾内容的发生时间与相关检查结果的对应关系未确认。'

# Issue codes for receipt-only findings that a server repair can resolve.
ACTION_ROLE = 'action_role'
ACTION_PASSAGES = 'action_passages'
RECALL_TIMING = 'recall_timing'
REPAIRABLE_CODES = frozenset({ACTION_ROLE, ACTION_PASSAGES, RECALL_TIMING})

_CLAIM_CODES = (
    ('本P实际提出患者管理行动', ACTION_ROLE),
    ('同段同时采用日期确定的检查和日期不明的留样回顾', RECALL_TIMING),
)
_STATEMENT = re.compile(r'[^。！？；;\n]+[。！？；;\n]*|[。！？；;\n]+')
_CLAUSE_END = re.compile(r'[。！？；;\n]+$')
_PID = re.compile(r'^\s*P0*(\d+)\s*$')
_CLAIM_DETAIL = re.compile(r'^(?P<pid>P\d{2})/K\d{2}(?:（(?P<term>.+?)）)?：')
_LITERAL_DETAIL = re.compile(r'^(?P<pid>P\d{2})：(?:.*?「(?P<label>[^」]+)」)?')
_DIGIT = re.compile(r'\d')
_DEPENDENT_START = re.compile(
    r'^\s*(?:若|如果|如(?!何)|一旦|当|必要时|否则|则|仍|复查后|此后|随后|之后|届时|并|且|同时|另外|此外|其余|其中|'
    r'该|此|这|上述|前述|以上|因此|故|所以|但|而)'
)


def claim_issue_code(problem):
    """Machine code of a claim-authority message, or None for content issues."""
    for prefix, code in _CLAIM_CODES:
        if problem.startswith(prefix):
            return code
    return None


def renumber_cell_claims(parts):
    """Number claims once per cell, so a decision can never name a sibling's claim."""
    counter = 0
    for part in parts:
        for candidate in part.get('claimCandidates', ()):
            counter += 1
            candidate['id'] = f'K{counter:02d}'


def _canonical_pid(value, known):
    if isinstance(value, str) and value in known:
        return value
    match = _PID.match(value) if isinstance(value, str) else None
    if match:
        pid = f'P{int(match.group(1)):02d}'
        if pid in known:
            return pid
    return None


def _plain(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(text or '')))


def _locate_quote(quote, paragraph):
    """Return the exact source substring for a quote differing only in spacing/width."""
    for field in ('text', 'title'):
        value = paragraph.get(field) or ''
        if not value or not isinstance(quote, str) or not quote:
            continue
        if quote in value:
            return quote
        target = _plain(quote)
        if not target:
            continue
        # Map each plain character back to its position in the source text.
        positions, plain = [], []
        for index, char in enumerate(value):
            normal = _plain(char)
            for item in normal:
                plain.append(item)
                positions.append(index)
        joined = ''.join(plain)
        found = joined.find(target)
        if found >= 0:
            start = positions[found]
            end = positions[found + len(target) - 1] + 1
            return value[start:end]
    return None


def normalize_receipt(row, cell):
    """Correct receipt bookkeeping for one cell; returns ``(row, corrections)``.

    ``cell`` is the private validator cell with immutable paragraphs.
    """
    corrections = []
    if not isinstance(row, dict) or not isinstance(row.get('paragraphs'), list):
        return row, corrections
    row = deepcopy(row)
    paragraphs = cell['paragraphs']
    owner_of = {
        candidate['id']: pid for pid, part in paragraphs.items() for candidate in part.get('claimCandidates', ())
    }
    merged, order = {}, []
    for part in row['paragraphs']:
        if not isinstance(part, dict):
            merged.setdefault(None, []).append(part)
            continue
        pid = _canonical_pid(part.get('id'), paragraphs)
        if pid is None:
            corrections.append({'type': 'unknown_paragraph_dropped', 'id': part.get('id')})
            continue
        if pid != part.get('id'):
            corrections.append({'type': 'paragraph_id_normalized', 'from': part.get('id'), 'to': pid})
            part = {**part, 'id': pid}
        if pid not in merged:
            merged[pid] = part
            order.append(pid)
            continue
        # A repeated P entry: keep every reported violation and decision.
        first = merged[pid]
        extra = [item for item in part.get('violations') or () if item not in (first.get('violations') or [])]
        if extra:
            first['violations'] = [*(first.get('violations') or []), *extra]
            first['sourceVerdict'] = 'unsupported'
        if isinstance(part.get('claimDecisions'), list):
            if not isinstance(first.get('claimDecisions'), list):
                first['claimDecisions'] = []
            first['claimDecisions'].extend(part['claimDecisions'])
        corrections.append({'type': 'duplicate_paragraph_merged', 'id': pid})
    # Decisions belong to the P that owns the K; K ids are unique in a cell.
    placed = {pid: [] for pid in order}
    seen = set()
    for pid in order:
        decisions = merged[pid].get('claimDecisions')
        if not isinstance(decisions, list):
            continue
        for decision in decisions:
            kid = decision.get('id') if isinstance(decision, dict) else None
            owner = owner_of.get(kid)
            if owner is None:
                corrections.append({'type': 'unknown_claim_dropped', 'paragraph': pid, 'id': kid})
                continue
            if kid in seen:
                corrections.append({'type': 'duplicate_claim_dropped', 'paragraph': pid, 'id': kid})
                continue
            if owner not in placed:
                # The owner P has no receipt entry; the validator reports it.
                corrections.append({'type': 'claim_owner_missing', 'paragraph': pid, 'id': kid})
                continue
            seen.add(kid)
            decision = dict(decision)
            if owner != pid:
                corrections.append({'type': 'claim_moved', 'from': pid, 'to': owner, 'id': kid})
            allowed_passages = paragraphs[owner].get('passageIds') or []
            if 'passageIds' in decision:
                passages = decision['passageIds']
                if decision.get('use') != 'patient_management':
                    if passages:
                        corrections.append({'type': 'claim_passages_dropped', 'id': kid})
                    decision.pop('passageIds')
                elif isinstance(passages, list):
                    kept = [pid_ for pid_ in passages if isinstance(pid_, str) and pid_ in allowed_passages]
                    if kept != passages:
                        corrections.append({'type': 'claim_passages_limited', 'id': kid})
                        decision['passageIds'] = list(dict.fromkeys(kept))
            if 'observationIds' in decision and not isinstance(decision['observationIds'], list):
                decision['observationIds'] = []
            placed[owner].append(decision)
    for pid in order:
        if isinstance(merged[pid].get('claimDecisions'), list) or placed[pid]:
            merged[pid]['claimDecisions'] = placed[pid]
    # Violations: exact quotes in their own P, limited to that P's sources.
    moved = {pid: [] for pid in order}
    emptied = set()
    for pid in order:
        part = merged[pid]
        violations = part.get('violations')
        if not isinstance(violations, list):
            continue
        kept = []
        for violation in violations:
            if not isinstance(violation, dict):
                kept.append(violation)
                continue
            violation = dict(violation)
            quote = violation.get('quote')
            owner = pid
            exact = _locate_quote(quote, paragraphs[pid])
            if exact is None:
                for other in order:
                    if other != pid and (exact := _locate_quote(quote, paragraphs[other])) is not None:
                        owner = other
                        break
            if exact is not None and exact != quote:
                corrections.append({'type': 'violation_quote_normalized', 'paragraph': owner})
                violation['quote'] = exact
            source = paragraphs[owner]
            for field, allowed in (
                ('recordIds', source.get('recordIds') or []),
                ('passageIds', source.get('passageIds') or []),
            ):
                values = violation.get(field)
                limited = (
                    [value for value in values if isinstance(value, str) and value in allowed]
                    if isinstance(values, list)
                    else []
                )
                if limited != values:
                    corrections.append({'type': f'violation_{field}_limited', 'paragraph': owner})
                    violation[field] = limited
            if owner == pid:
                kept.append(violation)
            else:
                corrections.append({'type': 'violation_moved', 'from': pid, 'to': owner})
                moved[owner].append(violation)
        if violations and not kept:
            emptied.add(pid)
        part['violations'] = kept
    for pid, extra in moved.items():
        if extra:
            merged[pid]['violations'] = [*(merged[pid].get('violations') or []), *extra]
    for pid in order:
        part = merged[pid]
        violations = part.get('violations')
        if isinstance(violations, list) and violations and part.get('sourceVerdict') == 'supported':
            # A reported error outweighs a contradictory verdict word.
            part['sourceVerdict'] = 'unsupported'
            corrections.append({'type': 'verdict_follows_violations', 'paragraph': pid})
        elif not violations and pid in emptied and part.get('sourceVerdict') == 'unsupported':
            # Its only reported errors quote a sibling P and moved there.
            part['sourceVerdict'] = 'supported'
    row['paragraphs'] = [merged[pid] for pid in order] + list(merged.get(None, []))
    return row, corrections


def _statements(text):
    return [match.group() for match in _STATEMENT.finditer(text or '') if match.group()]


def _close(text):
    text = text.strip()
    if not text:
        return text
    text = re.sub(r'[；;，,、]+$', '', text)
    return text if _CLAUSE_END.search(text) else text + '。'


def _statement_indexes(statements, start, length):
    """Indexes of statements overlapping ``[start, start + length)`` in the joined text."""
    found, offset = [], 0
    for index, statement in enumerate(statements):
        end = offset + len(statement)
        if start < end and start + max(1, length) > offset:
            found.append(index)
        offset = end
    return found


def _locate_term(text, term, start=None):
    if start is not None and 0 <= start and text[start : start + len(term)] == term:
        return start
    position = text.find(term)
    return position if position >= 0 else None


def recall_timing_targets(private_cell):
    """P ids whose recalled specimen event lacks an explicit unconfirmed relation."""
    targets = []
    for pid, part in private_cell['paragraphs'].items():
        if any(
            candidate['kind'] == 'event_relation'
            and not candidate.get('timingExplicit')
            and not candidate.get('affirmedSameSpecimen')
            and not candidate.get('affirmedDifferentEvent')
            for candidate in part.get('claimCandidates', ())
        ):
            targets.append(pid)
    return targets


def add_recall_timing_note(paragraphs, pids):
    """Append the fixed unconfirmed-timing sentence to the given P ids."""
    updated = [dict(part) for part in paragraphs]
    changed = []
    for pid in pids:
        index = int(pid[1:]) - 1
        if not 0 <= index < len(updated) or RECALL_TIMING_NOTE in updated[index]['text']:
            continue
        updated[index]['text'] = _close(updated[index]['text']) + RECALL_TIMING_NOTE
        changed.append(pid)
    return updated, changed


def repair_receipt_findings(paragraphs, issues, private_cell):
    """Apply server repairs for receipt-only findings of one cell.

    ``issues`` are the validator's semantic issues for the cell; all must carry
    a repairable code. Returns ``(paragraphs, notes, origins)`` where
    ``origins[i]`` is the original P index of new paragraph ``i``, or ``None``
    when the findings cannot be repaired deterministically.
    """
    if not issues or any(issue.get('code') not in REPAIRABLE_CODES for issue in issues):
        return None
    actions, passages, timing = {}, {}, []
    for issue in issues:
        pid = issue.get('paragraph')
        if pid not in private_cell['paragraphs']:
            return None
        if issue['code'] == ACTION_ROLE:
            actions.setdefault(pid, []).append(issue)
        elif issue['code'] == ACTION_PASSAGES:
            passages.setdefault(pid, set()).update(issue.get('passageIds') or ())
        else:
            timing.append(pid)
    notes = []
    updated, changed = add_recall_timing_note(paragraphs, timing)
    if changed:
        notes.append({'repair': RECALL_TIMING, 'paragraphs': changed})
    result, origins = [], []
    for index, part in enumerate(updated):
        pid = f'P{index + 1:02d}'
        part = dict(part)
        if pid in passages:
            part['cite'] = [value for value in part.get('cite', ()) if value not in passages[pid]]
            notes.append({'repair': ACTION_PASSAGES, 'paragraph': pid, 'removedPassages': sorted(passages[pid])})
        if pid not in actions:
            result.append(part)
            origins.append(index)
            continue
        statements = _statements(part['text'])
        moved = set()
        for issue in actions[pid]:
            term = issue.get('term') or ''
            start = _locate_term(part['text'], term, issue.get('start'))
            if not term or start is None:
                return None
            moved.update(_statement_indexes(statements, start, len(term)))
        if not moved:
            return None
        remainder = ''.join(statement for position, statement in enumerate(statements) if position not in moved)
        action_text = _close(''.join(statements[position] for position in sorted(moved)))
        if not re.sub(r'[\W_]+', '', remainder):
            result.append({**part, 'text': action_text, 'role': 'action', 'cite': []})
            origins.append(index)
            notes.append({'repair': ACTION_ROLE, 'paragraph': pid, 'mode': 'relabel'})
            continue
        result.append({**part, 'text': _close(remainder)})
        origins.append(index)
        result.append(
            {
                'text': action_text,
                'factIds': list(part.get('factIds') or ()),
                'cite': [],
                'role': 'action',
            }
        )
        origins.append(index)
        notes.append({'repair': ACTION_ROLE, 'paragraph': pid, 'mode': 'split', 'actionText': action_text})
    return result, notes, origins


def _term_key(value):
    return _plain(value).casefold()


def repaired_receipt(old_receipt, old_private, new_private, origins):
    """Carry the reviewer's decisions to repaired paragraphs, or None if any K is unmatched."""
    old_parts = {part.get('id'): part for part in old_receipt.get('paragraphs', ()) if isinstance(part, dict)}
    old_ids = list(old_private['paragraphs'])
    paragraphs = []
    for index, (pid, part) in enumerate(new_private['paragraphs'].items()):
        old_pid = old_ids[origins[index]]
        old_candidates = {
            candidate['id']: candidate for candidate in old_private['paragraphs'][old_pid].get('claimCandidates', ())
        }
        old_decisions = [
            (old_candidates[decision['id']], decision)
            for decision in (old_parts.get(old_pid, {}).get('claimDecisions') or ())
            if isinstance(decision, dict) and decision.get('id') in old_candidates
        ]
        decisions = []
        for candidate in part.get('claimCandidates', ()):
            key = _term_key(candidate.get('term'))
            match = next(
                (
                    decision
                    for old, decision in old_decisions
                    if old['kind'] == candidate['kind'] and _term_key(old.get('term')) == key
                ),
                None,
            ) or next(
                (
                    decision
                    for old, decision in old_decisions
                    if old['kind'] == candidate['kind']
                    and key
                    and (key in _term_key(old.get('term')) or _term_key(old.get('term')) in key)
                ),
                None,
            )
            if match is None:
                return None
            decision = {**deepcopy(match), 'id': candidate['id']}
            if 'passageIds' in decision:
                decision['passageIds'] = [value for value in decision['passageIds'] if value in part['passageIds']]
            decisions.append(decision)
        paragraphs.append({'id': pid, 'sourceVerdict': 'supported', 'violations': [], 'claimDecisions': decisions})
    return {'id': old_receipt.get('id'), 'paragraphs': paragraphs}


def offending_spans(text, pid, issues):
    """Substrings of one P that the review rejected, from server or reviewer findings."""
    spans = []
    for issue in issues:
        detail = issue.get('detail') if isinstance(issue, dict) else None
        if not isinstance(detail, str) or not detail:
            continue
        if claim := _CLAIM_DETAIL.match(detail):
            if claim['pid'] == pid:
                spans.append(claim['term'] or None)
            continue
        if literal := _LITERAL_DETAIL.match(detail):
            if literal['pid'] == pid:
                spans.append(literal['label'] or None)
            continue
        # Reviewer violation: "<exact quote>：<reason>". The quote may itself
        # contain a colon; take the longest prefix that occurs in this P.
        cuts = [match.start() for match in re.finditer('：', detail)]
        for cut in sorted(cuts, reverse=True):
            quote = detail[:cut]
            if len(quote) >= 4 and quote in text:
                spans.append(quote)
                break
    return spans


def trim_rejected_clauses(text, spans):
    """Remove the statements containing each rejected span; None means drop the P."""
    if None in spans:
        return None
    statements = _statements(text)
    removed = set()
    for span in spans:
        start = text.find(span)
        if start < 0:
            return None
        removed.update(_statement_indexes(statements, start, len(span)))
    # A statement that continues or conditions a removed one ("复查后仍阳性时……",
    # "该结果……") cannot stand alone; it goes with its antecedent.
    for index in range(1, len(statements)):
        if index - 1 in removed and _DEPENDENT_START.match(statements[index]):
            removed.add(index)
    kept = ''.join(statement for index, statement in enumerate(statements) if index not in removed)
    kept = _close(kept)
    return kept if re.sub(r'[\W_]+', '', kept) else ''


_CELL_LEVEL_PASSAGES = '该行动段有E未能支持'
_CELL_LEVEL_ACTION = '本格承担个人行动职责'


def _issue_paragraph(detail, paragraphs):
    """The P an issue belongs to, ``'cell'`` for known cell-level findings, else None."""
    if match := _CLAIM_DETAIL.match(detail) or _LITERAL_DETAIL.match(detail):
        return match['pid']
    if detail.startswith((_CELL_LEVEL_PASSAGES, _CELL_LEVEL_ACTION)):
        return 'cell'
    for cut in sorted((match.start() for match in re.finditer('：', detail)), reverse=True):
        quote = detail[:cut]
        if len(quote) < 4:
            continue
        for index, part in enumerate(paragraphs, 1):
            if quote in part['text']:
                return f'P{index:02d}'
    return None


def retained_paragraphs(paragraphs, statuses, issues):
    """Keep reviewed paragraphs minus the clauses the review rejected.

    Only paragraphs that passed medical review and were source-checked keep
    their text: ``supported`` ones whole, ``source_error`` ones without the
    rejected statements. Every finding must be attributable to a paragraph or
    be a known cell-level finding; otherwise nothing is retained. Returns
    ``(paragraphs, removed, origins)`` with the original index of each kept P.
    """
    kept, removed, origins = [], [], []
    if not statuses:
        return kept, removed, origins
    details = [issue.get('detail') for issue in issues if isinstance(issue, dict)]
    if any(not isinstance(detail, str) or _issue_paragraph(detail, paragraphs) is None for detail in details):
        return kept, removed, origins
    cell_level = any(detail.startswith(_CELL_LEVEL_PASSAGES) for detail in details)
    for index, part in enumerate(paragraphs, 1):
        pid = f'P{index:02d}'
        status = statuses.get(pid)
        if status == 'supported':
            text = part['text']
        elif status == 'source_error':
            spans = offending_spans(part['text'], pid, issues)
            text = trim_rejected_clauses(part['text'], spans) if spans else None
        else:
            text = None
        if not text:
            removed.append({'paragraph': pid, 'status': status, 'text': part['text']})
            continue
        if text != part['text']:
            removed.append({'paragraph': pid, 'status': status, 'text': part['text'], 'kept': text})
        row = {**part, 'text': text}
        if cell_level and row.get('role') == 'action' and row.get('cite'):
            removed.append({'paragraph': pid, 'removedPassages': list(row['cite'])})
            row['cite'] = []
        kept.append(row)
        origins.append(index - 1)
    return kept, removed, origins


def has_number(paragraphs):
    return any(_DIGIT.search(part['text']) for part in paragraphs)


_TRAILING_PUNCTUATION = re.compile(r'[\s。．.！？!?；;，,、：:]+$')
_NAME_END = re.compile(r'[A-Za-z0-9）)\]%＋+\-]$')
_VALUE_START = re.compile(r'^[A-Za-z0-9<>≤≥＜＞+\-−.]')


def _fact_value(fact):
    value = _TRAILING_PUNCTUATION.sub('', str(fact.get('value') or '').strip())
    unit = str(fact.get('unit') or '').strip()
    if value and unit and not value.endswith(unit):
        value = f'{value} {unit}'
    return value


def _joined_name_value(name, value):
    """Keep a name ending in letters or digits apart from a value starting with them (CA125 6.3)."""
    return f'{name} {value}' if _NAME_END.search(name) and _VALUE_START.match(value) else f'{name}{value}'


def source_only_text(facts, *, limit=8):
    """A reader sentence listing recorded values, references and source states only."""
    pieces = []
    for fact in facts:
        name = _TRAILING_PUNCTUATION.sub('', re.sub(r'\s+', ' ', str(fact.get('name') or '')).strip())
        value = _fact_value(fact)
        if not name or not value:
            continue
        details = []
        reference = _TRAILING_PUNCTUATION.sub('', str(fact.get('referenceRange') or fact.get('reference_range') or ''))
        if reference.strip():
            details.append(f'参考{reference.strip()}')
        status = _TRAILING_PUNCTUATION.sub('', str(fact.get('status') or '').strip())
        if status and status not in value:
            details.append(status)
        pieces.append(_joined_name_value(name, value) + (f'（{"，".join(details)}）' if details else ''))
        if len(pieces) >= limit:
            break
    if not pieces:
        return ''
    return '；'.join(pieces) + '。'


__all__ = [
    'ACTION_PASSAGES',
    'ACTION_ROLE',
    'RECALL_TIMING',
    'RECALL_TIMING_NOTE',
    'REPAIRABLE_CODES',
    'add_recall_timing_note',
    'claim_issue_code',
    'has_number',
    'normalize_receipt',
    'offending_spans',
    'recall_timing_targets',
    'renumber_cell_claims',
    'repair_receipt_findings',
    'repaired_receipt',
    'retained_paragraphs',
    'source_only_text',
    'trim_rejected_clauses',
]
