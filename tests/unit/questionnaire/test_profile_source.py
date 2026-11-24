"""Fixed raw acceptance, legacy identity selection and response independence."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.questionnaire.context import answer_rows
from modules.questionnaire.profile_source import LEGACY_SELECTION
from modules.questionnaire.profile_source import QuestionnaireSourceConflict
from modules.questionnaire.profile_source import acceptance_selection
from modules.questionnaire.profile_source import answer_evidence_v1
from modules.questionnaire.profile_source import execution_selection
from modules.questionnaire.profile_source import raw_submission
from modules.questionnaire.profile_source import saved_binding
from modules.questionnaire.profile_source import source_digest
from tests.unit.questionnaire.source_fakes import submission
from tests.unit.questionnaire.test_lifecycle import harness as lifecycle_harness
from tests.unit.questionnaire.test_lifecycle import ready


def test_raw_answers_and_schema_order_are_preserved_and_legacy_mapping_is_equal():
    source = submission()
    raw = raw_submission(source)
    assert raw['rawAnswers']['z_note'] == '  原始文字  '
    expected = [
        {key: value for key, value in row.items() if key != 'sourceKind'} | {'provided': True}
        for row in answer_rows(source)
    ]
    assert answer_evidence_v1(raw) == expected
    # Chunk JSON has canonical object-key order; survey array order must still win.
    raw['answers'] = dict(sorted(raw['answers'].items()))
    assert answer_evidence_v1(raw) == expected
    assert saved_binding(source)['draftDigest'] == source_digest(raw)
    changed = deepcopy(source)
    changed.update(revision='changed', factorAssessment={'status': 'completed'})
    assert saved_binding(changed) == saved_binding(source)


@pytest.mark.parametrize('field', ['rawAnswers', 'canonicalAnswers', 'answers', 'reports', 'artifacts', 'submittedAt'])
def test_saved_raw_and_attachment_inventory_corruption_is_rejected(field):
    source = submission()
    source[field] = 'changed'
    with pytest.raises((QuestionnaireSourceConflict, TypeError, ValueError)):
        saved_binding(source)


@pytest.mark.parametrize('user_id', ['u' * 257, 'u' * 512, ' u', 'u '])
def test_scope_incompatible_existing_identity_is_fixed_legacy_before_acceptance(user_id):
    assert acceptance_selection(user_id, execution_selection()) == LEGACY_SELECTION
    assert acceptance_selection('u' * 256, execution_selection()) != LEGACY_SELECTION


@pytest.mark.parametrize('mode', ['future', 1, True, {}, []])
def test_unknown_delivery_mode_never_silently_becomes_legacy(mode):
    source = submission()
    source['profileIngestion']['deliveryMode'] = mode
    with pytest.raises(QuestionnaireSourceConflict):
        saved_binding(source)


@pytest.mark.asyncio
async def test_business_submission_succeeds_when_local_registration_is_unavailable():
    h = lifecycle_harness.__wrapped__()
    queued = AsyncMock(side_effect=RuntimeError('local publication store unavailable'))
    h.service.profile_source_delivery = SimpleNamespace(execution_selection=execution_selection, ensure_queued=queued)
    source = await ready(h)
    result = await h.service.submit('user', source['questionnaireId'], {'note': '  原文  '})
    assert result['status'] == 'submitted' and result['answers'] == {'note': '原文'}
    stored = await h.store.get_submitted('user', source['questionnaireId'])
    assert saved_binding(stored) and stored['rawAnswers'] == {'note': '  原文  '}
    h.delivery.assert_not_awaited()
    queued.assert_awaited_once()
    replay = await h.service.submit('user', source['questionnaireId'], {'note': '原文'})
    assert replay == result and queued.await_count == 1


@pytest.mark.asyncio
async def test_previously_accepted_legacy_questionnaire_does_not_switch_at_submission():
    h = lifecycle_harness.__wrapped__()
    source = await ready(h)
    queued = AsyncMock()
    h.service.profile_source_delivery = SimpleNamespace(execution_selection=execution_selection, ensure_queued=queued)
    await h.service.submit('user', source['questionnaireId'], {'note': 'original'})
    assert saved_binding(await h.store.get_submitted('user', source['questionnaireId'])) is None
    h.delivery.assert_awaited_once()
    queued.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_long_identity_keeps_actual_legacy_submission_path():
    h = lifecycle_harness.__wrapped__()
    queued = AsyncMock()
    h.service.profile_source_delivery = SimpleNamespace(execution_selection=execution_selection, ensure_queued=queued)
    user = 'u' * 512
    created = await h.service.create(user, 'session')
    await asyncio.gather(*list(h.service._tasks.values()))
    await h.service.submit(user, created['questionnaireId'], {'note': 'original'})
    source = await h.store.get_submitted(user, created['questionnaireId'])
    assert source['profileSourceSelection'] == LEGACY_SELECTION and saved_binding(source) is None
    h.delivery.assert_awaited_once_with(user_id=user, questionnaire_id=created['questionnaireId'])
    queued.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_business_write_never_registers_and_background_commit_keeps_fixed_intent():
    h = lifecycle_harness.__wrapped__()
    queued = AsyncMock()
    h.service.profile_source_delivery = SimpleNamespace(execution_selection=execution_selection, ensure_queued=queued)
    source = await ready(h)
    h.store.fail_submission_once = True
    with pytest.raises(RuntimeError):
        await h.service.submit('user', source['questionnaireId'], {'note': '  original  '})
    queued.assert_not_awaited()
    saved = await h.store.get_by_id('user', source['questionnaireId'])
    assert saved['status'] == 'ready' and saved['pendingSubmission']['rawAnswers'] == {'note': '  original  '}
    await h.service.recover_pending()
    committed = await h.store.get_submitted('user', source['questionnaireId'])
    assert saved_binding(committed) and committed['rawAnswers'] == {'note': '  original  '}
