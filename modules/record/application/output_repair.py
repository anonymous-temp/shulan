"""Collect independently valid Record output without guessing identities or facts.

Collectors are scoped to one generation and its bounded repair. They retain good
items when a repaired reply omits them. An item is accepted only after the same
schema, source and ownership rules used by the normal flow have passed.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from agents.llm.text_quality import check_generated_value
from modules.record.application import intake as intake_policy
from modules.record.application import interview as interview_policy
from modules.record.application.flow import ConcernAssessment
from modules.record.application.flow import ConcernResponse
from modules.record.application.flow import GeneratedQuestion
from modules.record.application.flow import InformationGap
from modules.record.application.flow import PatientAssessment
from modules.record.application.flow import PatientMessage
from modules.record.application.flow import TurnPresentation
from modules.record.application.flow import validate_assessment
from modules.record.application.flow import validate_concern_source
from modules.record.application.flow import validate_new_question
from modules.record.application.intake import ComplaintObservation
from modules.record.application.intake import IntakeFact
from modules.record.application.intake import IntakeUnderstanding
from modules.record.application.presentation import choice_context_options
from modules.record.application.presentation import question_task
from modules.record.application.presentation import validate_answer_entry_instructions
from modules.record.application.presentation import validate_copy_components
from modules.record.application.presentation import validate_message
from modules.record.application.presentation import validate_response_style

_MISSING = object()
_EMPTY_INTAKE = {'complaints': [], 'facts': []}
_EMPTY_ASSESSMENT = {
    'intent': 'answer',
    'substantive': False,
    'intakeSufficient': False,
    'summary': '等待补充信息。',
    'concerns': [],
    'gaps': [],
    'intake': _EMPTY_INTAKE,
}


def _object(content: str) -> dict:
    text = content.strip()
    if text.startswith('```'):
        lines = text.splitlines()
        if len(lines) < 3 or lines[-1].strip() != '```':
            raise ValueError('unclosed_json_fence')
        text = '\n'.join(lines[1:-1])
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError('object_required')
    return value


def _field(value: dict, schema, name: str):
    """Ambiguous simultaneous aliases are not a repairable spelling variation."""
    alias = schema.model_fields[name].validation_alias
    names = list(dict.fromkeys([name, *(getattr(alias, 'choices', []) or [])]))
    present = [key for key in names if key in value]
    return value[present[0]] if len(present) == 1 else _MISSING


def _checked(value, schema, source_text):
    check_generated_value(value, chinese_context=True, source_text=source_text)
    return schema.model_validate(value)


def _fingerprint(value) -> str:
    return json.dumps(value.model_dump(), sort_keys=True, ensure_ascii=False)


class AnalysisRepair:
    """Preserve source-backed analysis pieces; never manufacture missing controls."""

    def __init__(self, session: dict, answer, *, sources: dict | None = None, source_text: str = ''):
        self.session = session
        self.answer = answer
        self.sources = sources or {}
        self.source_text = source_text
        self._controls = {}
        self._concerns = {}
        self._complaints = {}
        self._facts = []
        self._gaps = []
        self._state = intake_policy.normalize_state(session['qaFlow'])
        self._priority = bool(self._state.get('priorityClarificationNeeded', False))
        self._priority_supplied = False
        self._background_consent = 'unspecified'
        self._preferred_key = None
        self._seen = set()
        self._invalid = {}
        self._unknown_ids = set()
        self._empty_intake_seen = False
        self._requested_gap = False
        self.last_complete = False
        self._local_primary_keys = set()
        self._last_content = None
        self._last_result = None

    def _reject(self, field):
        self._invalid[field] = self._invalid.get(field, 0) + 1
        self.last_complete = False

    def _understanding(self, *, facts=None) -> IntakeUnderstanding:
        keys = list(self._complaints)
        return IntakeUnderstanding(
            complaints=list(self._complaints.values()),
            facts=self._facts if facts is None else facts,
            priorityClarificationNeeded=self._priority,
            preferredIndex=keys.index(self._preferred_key) if self._preferred_key in keys else None,
            backgroundConsent=self._background_consent,
        )

    @property
    def partial_intake(self) -> IntakeUnderstanding | None:
        if not (self._complaints or self._facts or self._empty_intake_seen):
            return None
        try:
            value = self._understanding()
            intake_policy.validate_understanding(value, self.session, self.answer, self.sources)
            return value.model_copy(deep=True)
        except ValueError:
            return None

    @property
    def concerns(self) -> list[ConcernAssessment]:
        return [item.model_copy(deep=True) for item in self._concerns.values()]

    @property
    def gaps(self) -> list[InformationGap]:
        return [item.model_copy(deep=True) for item in self._gaps]

    @property
    def is_partial(self) -> bool:
        return bool(self._invalid)

    @property
    def diagnostics(self) -> dict:
        required = {item['id'] for item in self.session['qaFlow']['concerns'] if item['status'] == 'pending'}
        actual = {item.id for item in self._concerns.values() if item.id}
        return {
            'invalidCounts': dict(self._invalid),
            'missingFields': sorted({'intent', 'substantive', 'intakeSufficient', 'summary'} - self._controls.keys()),
            'missingConcernIds': sorted(required - actual),
            'unknownConcernIdHashes': sorted(self._unknown_ids)[:8],
            'acceptedCounts': {
                'concerns': len(self._concerns),
                'complaints': len(self._complaints),
                'facts': len(self._facts),
                'gaps': len(self._gaps),
            },
        }

    def ingest(self, content: str) -> PatientAssessment | None:
        # Service validation can inspect the same rejected reply twice. That is
        # not a new repair and must not resolve an earlier conflict by accident.
        if content == self._last_content:
            return self._last_result.model_copy(deep=True) if self._last_result is not None else None
        result = self._ingest(content)
        self._last_content = content
        self._last_result = result.model_copy(deep=True) if result is not None else None
        return result

    def _ingest(self, content: str) -> PatientAssessment | None:
        self.last_complete = False
        try:
            raw = _object(content)
        except (ValueError, TypeError):
            self._reject('json')
            return None
        try:
            complete = _checked(raw, PatientAssessment, self.source_text)
            validate_assessment(complete, self.session, self.answer, sources=self.sources)
            self.last_complete = True
        except ValueError:
            pass
        for name in ('intent', 'substantive', 'intakeSufficient', 'summary'):
            value = _field(raw, PatientAssessment, name)
            if value is _MISSING:
                self._reject(name)
                continue
            try:
                parsed = _checked({**_EMPTY_ASSESSMENT, name: value}, PatientAssessment, self.source_text)
                if self.last_complete:
                    # A fully validated repair may correct a control selected
                    # by an otherwise rejected reply, particularly stop intent.
                    self._controls[name] = getattr(parsed, name)
                else:
                    self._controls.setdefault(name, getattr(parsed, name))
            except ValueError:
                self._reject(name)
        self._collect_concerns(_field(raw, PatientAssessment, 'concerns'))
        local_keys = self._collect_intake(_field(raw, PatientAssessment, 'intake'))
        self._collect_gaps(_field(raw, PatientAssessment, 'gaps'), local_keys)
        if len(self._controls) != 4 or not {'concerns', 'gaps', 'complaints', 'facts'}.issubset(self._seen):
            return None
        if self._requested_gap and not self._gaps and not self.last_complete:
            return None
        understanding = self.partial_intake
        if understanding is None:
            return None
        try:
            result = PatientAssessment(
                **{**self._controls, 'intakeSufficient': self._controls['intakeSufficient'] and not self._invalid},
                concerns=self.concerns,
                gaps=self.gaps,
                intake=understanding,
            )
            validate_assessment(result, self.session, self.answer, sources=self.sources)
            return result
        except ValueError:
            return None

    def _collect_concerns(self, values):
        if not isinstance(values, list):
            self._reject('concerns')
            return
        self._seen.add('concerns')
        known = {item['id'] for item in self.session['qaFlow']['concerns']}
        grouped = {}
        for raw in values:
            try:
                item = _checked(raw, ConcernAssessment, self.source_text)
                if item.id and item.id not in known:
                    self._unknown_ids.add(hashlib.sha256(item.id.encode('utf-8')).hexdigest()[:16])
                    raise ValueError('unknown_concern')
                if not item.id and not any(text.strip() for text in intake_policy._fragments(self.answer)):
                    raise ValueError('concern_source_unavailable')
                validate_concern_source(item, self.session, self.answer)
                key = ('id', item.id) if item.id else ('text', item.text, item.evidence)
                grouped.setdefault(key, []).append(item)
            except ValueError:
                self._reject('concerns')
        for key, items in grouped.items():
            if len({_fingerprint(item) for item in items}) != 1:
                self._reject('concerns')
                continue
            item = items[0]
            previous = self._concerns.get(key)
            if previous is None:
                self._concerns[key] = item
            elif self.last_complete:
                self._concerns[key] = previous.model_copy(
                    update={
                        'readyToAnswer': item.readyToAnswer,
                        'informationNeeded': item.informationNeeded,
                    }
                )
            elif previous.readyToAnswer != item.readyToAnswer:
                # A partial repair cannot turn contradictory readiness into a
                # reliable answer. Keep the validated request for clarification.
                self._reject('concerns')
                if not item.readyToAnswer:
                    self._concerns[key] = previous.model_copy(
                        update={
                            'readyToAnswer': False,
                            'informationNeeded': item.informationNeeded,
                        }
                    )

    def _collect_intake(self, raw):
        self._local_primary_keys = set()
        if not isinstance(raw, dict):
            self._reject('intake')
            return {}
        consent = _field(raw, IntakeUnderstanding, 'backgroundConsent')
        if consent is not _MISSING:
            try:
                probe = IntakeUnderstanding(complaints=[], facts=[], backgroundConsent=consent)
                intake_policy.validate_understanding(probe, self.session, self.answer, self.sources)
                if self._background_consent != 'declined' and consent != 'unspecified':
                    self._background_consent = consent
            except ValueError:
                self._reject('backgroundConsent')
        try:
            explicit = _checked(raw, IntakeUnderstanding, self.source_text)
            intake_policy.validate_understanding(explicit, self.session, self.answer, self.sources)
            if not explicit.complaints and not explicit.facts:
                self._empty_intake_seen = True
        except ValueError:
            pass
        values = _field(raw, IntakeUnderstanding, 'complaints')
        local_keys = {}
        grouped = {}
        if isinstance(values, list):
            self._seen.add('complaints')
            for index, value in enumerate(values):
                try:
                    item = _checked(value, ComplaintObservation, self.source_text)
                    probe = IntakeUnderstanding(complaints=[item], facts=[])
                    intake_policy.validate_understanding(probe, self.session, self.answer, self.sources)
                    existing = intake_policy._existing_complaint(self._state['complaints'], item)
                    key = ('id', existing['id']) if existing else ('text', item.text)
                    grouped.setdefault(key, []).append((index, item))
                except ValueError:
                    self._reject('complaints')
            for key, observations in grouped.items():
                if len({_fingerprint(item) for _, item in observations}) != 1:
                    self._reject('complaints')
                    continue
                if key not in self._complaints and len(self._complaints) >= 10:
                    self._reject('complaints')
                    continue
                if self.last_complete or key not in self._complaints:
                    self._complaints[key] = observations[0][1]
                if observations[0][1].primary:
                    self._local_primary_keys.add(key)
                for index, _ in observations:
                    local_keys[index] = key
        else:
            self._reject('complaints')
        preferred = _field(raw, IntakeUnderstanding, 'preferredIndex')
        if preferred is not _MISSING and preferred is not None:
            if type(preferred) is int and preferred in local_keys:
                if self._preferred_key is None:
                    self._preferred_key = local_keys[preferred]
            else:
                self._reject('preferredIndex')
        priority = _field(raw, IntakeUnderstanding, 'priorityClarificationNeeded')
        if priority is not _MISSING:
            if type(priority) is bool:
                if not self._priority_supplied:
                    self._priority = priority
                    self._priority_supplied = True
            else:
                self._reject('priorityClarificationNeeded')
        # Legacy implicit-primary items must not silently change ownership when
        # a later repair supplies an explicit complaint list.
        if self._complaints:
            for item in self._facts:
                if item.scope == 'primary' and not item.complaintIndexes:
                    self._reject('facts')
            for item in self._gaps:
                if item.scope == 'primary' and not item.complaintIndexes:
                    self._reject('gaps')
            self._facts = [item for item in self._facts if item.scope != 'primary' or item.complaintIndexes]
            self._gaps = [item for item in self._gaps if item.scope != 'primary' or item.complaintIndexes]
        facts = _field(raw, IntakeUnderstanding, 'facts')
        if not isinstance(facts, list):
            self._reject('facts')
            return local_keys
        self._seen.add('facts')
        candidates = []
        for value in facts:
            try:
                fact = self._bind(_checked(value, IntakeFact, self.source_text), local_keys)
                understanding = self._understanding(facts=[fact])
                intake_policy.validate_understanding(understanding, self.session, self.answer, self.sources)
                slots = self._fact_slots(understanding)
                candidates.append((fact, slots))
            except ValueError:
                self._reject('facts')
        states = {}
        for fact, slots in candidates:
            for slot in slots:
                states.setdefault(slot, set()).add(fact.status)
        if not self.last_complete:
            for fact in self._facts:
                for slot in self._fact_slots(self._understanding(facts=[fact])):
                    states.setdefault(slot, set()).add(fact.status)
        conflicting = {slot for slot, statuses in states.items() if len(statuses) > 1}
        if conflicting:
            self._reject('facts')
        # A fully valid repair owns the physical slots it explicitly supplies.
        # Its complete compatible group replaces an earlier interpretation of
        # those same slots, while unrelated accepted facts remain available.
        replaced = set().union(*(slots for _, slots in candidates)) if self.last_complete else conflicting
        retained = []
        for fact in self._facts:
            if kept := self._without_slots(fact, replaced):
                retained.append(kept)
        self._facts = retained
        for fact, slots in candidates:
            if slots & conflicting:
                self._reject('facts')
                fact = self._without_slots(fact, conflicting)
                if fact is None:
                    continue
            if _fingerprint(fact) in {_fingerprint(old) for old in self._facts}:
                continue
            try:
                combined = self._understanding(facts=[*self._facts, fact])
                intake_policy.validate_understanding(combined, self.session, self.answer, self.sources)
                self._facts.append(fact)
            except ValueError:
                self._reject('facts')
        return local_keys

    def _without_slots(self, fact: IntakeFact, removed: set[str]) -> IntakeFact | None:
        slots = self._fact_slots(self._understanding(facts=[fact]))
        if not slots & removed:
            return fact
        if not fact.complaintIndexes:
            return None
        # One fact may legitimately describe several complaints. Removing a
        # conflicting slot must not discard its independently valid siblings.
        indexes = [
            index
            for index in fact.complaintIndexes
            if not self._fact_slots(self._understanding(facts=[fact.model_copy(update={'complaintIndexes': [index]})]))
            & removed
        ]
        return fact.model_copy(update={'complaintIndexes': indexes}) if indexes else None

    def _fact_slots(self, understanding):
        ids = []
        for index, complaint in enumerate(understanding.complaints):
            existing = intake_policy._existing_complaint(self._state['complaints'], complaint)
            ids.append(existing['id'] if existing else f'new:{index}')
        return set(intake_policy._aggregate_facts(self._state, understanding, ids))

    def _bind(self, item, local_keys):
        indexes = item.complaintIndexes
        if len(indexes) != len(set(indexes)) or any(index not in local_keys for index in indexes):
            raise ValueError('unavailable_complaint_reference')
        keys = list(self._complaints)
        result = item.model_copy(deep=True)
        result.complaintIndexes = [keys.index(local_keys[index]) for index in indexes]
        if item.scope == 'primary' and not indexes and local_keys:
            marked = self._local_primary_keys
            targets = marked or set(local_keys.values())
            if len(targets) != 1:
                raise ValueError('ambiguous_complaint_reference')
            result.scope = 'complaint'
            result.complaintIndexes = [keys.index(next(iter(targets)))]
        elif item.scope == 'primary' and not indexes and self._complaints:
            raise ValueError('unavailable_complaint_reference')
        return result

    def _collect_gaps(self, values, local_keys):
        if not isinstance(values, list):
            self._reject('gaps')
            return
        self._seen.add('gaps')
        self._requested_gap = self._requested_gap or bool(values)
        for raw in values:
            try:
                gap = self._bind(_checked(raw, InformationGap, self.source_text), local_keys)
                understanding = self._understanding()
                intake_policy.validate_targets(understanding, gap)
                if self.session['qaFlow']['phase'] == 'intake' and (gap.dimension is None or not gap.reason):
                    raise ValueError('gap_purpose_required')
                if _fingerprint(gap) not in {_fingerprint(old) for old in self._gaps}:
                    self._gaps.append(gap)
            except ValueError:
                self._reject('gaps')


class PresentationRepair:
    """Collect response text exclusively by an already assigned concern ID."""

    def __init__(self, decision: dict, patient: dict, *, source_text: str = ''):
        self.decision = decision
        self.patient = patient
        self.source_text = source_text
        self.responses: dict[str, ConcernResponse] = {}
        self.assistant_message: PatientMessage | None = None
        self.question: GeneratedQuestion | None = None
        self._expected = {item['concernId']: item for item in decision['responses']}
        self._invalid = {}
        self._unknown_ids = set()
        self._conflicts = set()
        self._question_valid = decision['action'] == 'finish'
        if decision['action'] in ('invite_supplement', 'clarify_supplement', 'invite_background'):
            self.question = GeneratedQuestion.model_validate(
                decision.get('fixedQuestion')
                or question_task(decision['action'], decision.get('target'))['fixedQuestion']
            )
            self._question_valid = True
        if any('reuseResponse' in instruction for instruction in self._expected.values()):
            # Decisions are not persisted. A fresh reply must never copy a
            # historical answer, while already published receipts stay intact.
            self._reject('reusedResponses')

    def _reject(self, field):
        self._invalid[field] = self._invalid.get(field, 0) + 1

    def _check_response(self, response: ConcernResponse, *, options=None) -> None:
        instruction = self._expected[response.concernId]
        if 'reuseResponse' in instruction:
            raise ValueError('qa_response_reuse_forbidden')
        validate_answer_entry_instructions(response.text, options=options)
        validate_response_style(response.text, previous_response=instruction.get('previousResponse', ''))
        for previous in instruction.get('previousResponses', []):
            validate_response_style(response.text, previous_response=previous)

    def _check_components(self) -> None:
        """Remove only the offending piece, keeping independent good answers."""
        while True:
            ordered = [self.responses[key] for key in self._expected if key in self.responses]
            if not ordered and self._expected:
                return
            try:
                validate_copy_components(
                    self.assistant_message or SimpleNamespace(acknowledgement='', rationale=''),
                    ordered,
                    action=self.decision['action'],
                )
                return
            except ValueError as error:
                location = getattr(error, 'diagnostic_fields', {})
                index = location.get('responseIndex')
                if location.get('component') == 'responses' and isinstance(index, int) and 0 <= index < len(ordered):
                    self.responses.pop(ordered[index].concernId)
                    self._reject('responses')
                else:
                    self.assistant_message = None
                    self._reject('assistantMessage')
                    return

    @property
    def partial_presentation(self) -> dict:
        return {
            'responses': [value.model_copy(deep=True) for value in self.responses.values()],
            'assistantMessage': self.assistant_message.model_copy(deep=True) if self.assistant_message else None,
            'question': self.question.model_copy(deep=True) if self.question else None,
        }

    @property
    def diagnostics(self) -> dict:
        return {
            'expectedConcernIds': sorted(self._expected),
            'missingConcernIds': sorted(self._expected.keys() - self.responses.keys()),
            'conflictingConcernIds': sorted(self._conflicts),
            'invalidCounts': dict(self._invalid),
            'unknownConcernIdHashes': sorted(self._unknown_ids)[:8],
            'messageAccepted': self.assistant_message is not None,
            'questionAccepted': self._question_valid,
        }

    def ingest(self, content: str) -> TurnPresentation | None:
        try:
            raw = _object(content)
        except (ValueError, TypeError):
            self._reject('json')
            return None
        choices = (
            interview_policy.history_choice_fields(self.decision.get('target'))
            if self.decision['action'] in ('open_intake', 'ask_intake')
            else {}
        )
        if self.decision['action'] not in ('finish', 'invite_supplement', 'clarify_supplement', 'invite_background'):
            try:
                question = _checked(raw.get('question'), GeneratedQuestion, self.source_text)
                validate_new_question(question, action=self.decision['action'])
                validate_answer_entry_instructions(question.question, options=choices.get('options'))
                if self.question is None:
                    self.question = question
                    self._question_valid = True
            except ValueError:
                self._reject('question')
        options = choice_context_options(self.question.options if self.question else None, choices.get('options'))
        # A retained component can predate the choice labels that make its
        # input instruction unambiguously invalid. Remove it before accepting
        # replacements from this repair, without spending another model call.
        if self.assistant_message is not None:
            try:
                validate_message(
                    self.assistant_message,
                    self.patient,
                    action=self.decision['action'],
                    has_responses=bool(self._expected),
                )
                validate_answer_entry_instructions(self.assistant_message.acknowledgement, options=options)
                validate_answer_entry_instructions(self.assistant_message.rationale, options=options)
            except ValueError:
                self.assistant_message = None
                self._reject('assistantMessage')
        for identity, response in list(self.responses.items()):
            try:
                self._check_response(response, options=options)
            except ValueError:
                del self.responses[identity]
                self._reject('responses')
        if 'assistantMessage' in raw:
            try:
                message = _checked(raw['assistantMessage'], PatientMessage, self.source_text)
                validate_message(
                    message, self.patient, action=self.decision['action'], has_responses=bool(self._expected)
                )
                validate_answer_entry_instructions(message.acknowledgement, options=options)
                validate_answer_entry_instructions(message.rationale, options=options)
                if self.assistant_message is None:
                    self.assistant_message = message
            except ValueError:
                self._reject('assistantMessage')
        responses = raw.get('responses')
        if isinstance(responses, list):
            grouped = {}
            for value in responses:
                try:
                    item = _checked(value, ConcernResponse, self.source_text)
                    if item.concernId not in self._expected:
                        self._unknown_ids.add(hashlib.sha256(item.concernId.encode('utf-8')).hexdigest()[:16])
                        self._reject('unknownResponses')
                        continue
                    self._check_response(item, options=options)
                    grouped.setdefault(item.concernId, []).append(item)
                except ValueError:
                    self._reject('responses')
            for identity, items in grouped.items():
                if len({item.text for item in items}) != 1:
                    self._conflicts.add(identity)
                    self._reject('conflictingResponses')
                    continue
                self.responses.setdefault(identity, items[0])
                self._conflicts.discard(identity)
        else:
            self._reject('responses')
        self._check_components()
        if self.assistant_message is None or not self._question_valid or self.responses.keys() != self._expected.keys():
            return None
        return TurnPresentation(
            assistantMessage=self.assistant_message,
            responses=[self.responses[key] for key in self._expected],
            question=self.question,
        ).model_copy(deep=True)
