"""Questionnaire owner fence evidence with the real public Profile DTO boundary."""

from copy import deepcopy
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.questionnaire_source_admission import QuestionnaireSourceAdmissionAdapter
from modules.health_profile.contracts import ProfileFreshSourceStateV1
from modules.health_profile.contracts import ProfileScope
from modules.health_profile.contracts import ProfileSourcePresenceV1
from modules.health_profile.contracts import ProfileSourceSnapshotRefV1
from modules.questionnaire.profile_publication import source_task_id
from modules.questionnaire.profile_source import QuestionnaireSourceConflict
from tests.unit.questionnaire.source_fakes import environment
from tests.unit.questionnaire.source_fakes import save
from tests.unit.questionnaire.source_fakes import task


class FreshPort:
    """Explicit version-independent protocol fake; real V2 resolver integration is a later gate."""

    def __init__(self, resources):
        self.resources, self.calls, self.failure = resources, [], None

    async def reserve_fresh_source(self, authorization, *, scope):
        self.calls.append(('reserve', authorization.model_dump(mode='json', by_alias=True)))
        assert authorization.scope == scope
        proof = await self.resources.fence_reader.read_admission_fence(
            scope=scope, snapshot_ref=authorization.owner_fence_ref
        )
        assert proof['sourceId'] == authorization.source_id
        assert await self.resources.fence_reader.is_admission_fence_held(
            scope=scope, snapshot_ref=authorization.owner_fence_ref, content_digest=authorization.owner_fence_digest
        )
        await self.resources.snapshot_reader.read_source_snapshot(authorization.material_snapshot_ref, scope=scope)
        if self.failure == 'reserve':
            self.failure = None
            raise RuntimeError('reserve ACK lost')
        return ProfileFreshSourceStateV1(authorization=authorization, status='blocked', updated_at=datetime.now(UTC))

    async def authorize_fresh_source(self, authorization, *, scope):
        self.calls.append(('authorize', authorization.model_dump(mode='json', by_alias=True)))
        assert await self.resources.fence_reader.is_admission_fence_held(
            scope=scope, snapshot_ref=authorization.owner_fence_ref, content_digest=authorization.owner_fence_digest
        )
        if self.failure == 'authorize':
            self.failure = None
            raise RuntimeError('authorize ACK lost')
        return ProfileFreshSourceStateV1(authorization=authorization, status='ready', updated_at=datetime.now(UTC))


def compose():
    env = environment()
    scope = ProfileScope(user_id='u', subject_id='self')
    env.fresh = FreshPort(env.resources)
    env.presence = SimpleNamespace(
        read_legacy_presence=AsyncMock(
            return_value=ProfileSourcePresenceV1(
                scope=scope,
                source_type='questionnaire',
                source_id='q',
                status='absent',
                read_at=datetime.now(UTC),
                sections=tuple(
                    {'kind': key, 'observedCount': 0, 'evidenceDigest': '0' * 64}
                    for key in ('jobs', 'fact_contributions', 'publications')
                ),
                proof_ref='absence',
                content_digest='1' * 64,
            )
        )
    )
    env.inbox = SimpleNamespace(prepare_self_scope=AsyncMock(return_value=scope))
    env.service.admission = QuestionnaireSourceAdmissionAdapter(
        store=env.resources.store,
        owner=env.resources.owner,
        inbox=env.inbox,
        presence=env.presence,
        fresh_admission=env.fresh,
    )
    return env


@pytest.mark.asyncio
async def test_real_owner_adapter_signs_only_saved_new_mode_and_fixed_persistent_authorization():
    env = compose()
    await save(env)
    await env.service.recover_pending()
    current = await task(env)
    assert current['status'] == 'waiting_receipt'
    auth = current['authorization']['authorization']
    assert (
        auth['sourceType'] == 'questionnaire' and auth['materialContractVersion'] == 'profile.questionnaire-source.v2'
    )
    assert [kind for kind, value in env.fresh.calls] == ['reserve', 'authorize']
    stored = await env.resources.store.get(auth['authorizationId'])
    assert stored['authorization'] == auth and stored['presenceProof']['status'] == 'absent'
    assert 'snapshot' not in stored['material'] and 'answers' not in stored
    assert not env.source.indexes and not env.owned.indexes


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['reserve', 'authorize'])
async def test_lost_admission_ack_reuses_original_authorization_and_absence_evidence(phase):
    env = compose()
    await save(env)
    env.fresh.failure = phase
    await env.service.recover_pending()
    assert (await task(env))['status'] == 'retry_wait'
    first = deepcopy(env.fresh.calls[0][1])
    env.presence.read_legacy_presence.side_effect = AssertionError(
        'must not regenerate authorization from current history'
    )
    env.now[0] += 31
    await env.service.recover_pending()
    assert (await task(env))['status'] == 'waiting_receipt'
    assert all(value == first for _, value in env.fresh.calls)


@pytest.mark.asyncio
async def test_historical_fence_read_is_fixed_but_current_held_check_detects_raw_change():
    env = compose()
    await save(env)
    await env.service.recover_pending()
    auth = (await task(env))['authorization']['authorization']
    options = {
        'scope': ProfileScope.model_validate(auth['scope']),
        'snapshot_ref': ProfileSourceSnapshotRefV1.model_validate(auth['ownerFenceRef']),
    }
    before = await env.resources.fence_reader.read_admission_fence(**options)
    env.source.documents['q']['factorAssessment'] = {'status': 'completed'}
    assert await env.resources.fence_reader.is_admission_fence_held(**options, content_digest=auth['ownerFenceDigest'])
    env.source.documents['q']['rawAnswers']['z_note'] = 'changed'
    assert await env.resources.fence_reader.read_admission_fence(**options) == before
    with pytest.raises(QuestionnaireSourceConflict):
        await env.resources.fence_reader.is_admission_fence_held(**options, content_digest=auth['ownerFenceDigest'])


@pytest.mark.asyncio
@pytest.mark.parametrize('section', ['jobs', 'fact_contributions', 'publications'])
async def test_any_old_profile_history_refuses_fresh_owner_even_when_local_tasks_are_empty(section):
    env = compose()
    old = env.presence.read_legacy_presence.return_value
    value = old.model_dump(mode='json', by_alias=True)
    value['status'] = 'present'
    next(part for part in value['sections'] if part['kind'] == section)['observedCount'] = 1
    env.presence.read_legacy_presence.return_value = ProfileSourcePresenceV1.model_validate(value)
    await save(env)
    await env.service.recover_pending()
    assert (await task(env))['status'] == 'failed' and not env.fresh.calls
    assert not env.attachments.prepared and not env.receiver.events


@pytest.mark.asyncio
async def test_owned_formal_dispatch_without_corresponding_frozen_proof_is_not_fresh():
    env = compose()
    await save(env)
    await env.service.register(user_id='u', questionnaire_id='q')
    env.owned.documents[source_task_id('u', 'q')]['dispatchAttempts'] = 1
    await env.service.recover_pending()
    assert (await task(env))['status'] == 'failed' and not env.fresh.calls
    env.presence.read_legacy_presence.assert_not_awaited()


@pytest.mark.asyncio
async def test_absence_failure_retries_but_does_not_publish_or_invent_quiescence():
    env = compose()
    old = env.presence.read_legacy_presence.return_value
    env.presence.read_legacy_presence.return_value = old.model_copy(update={'status': 'unavailable'})
    await save(env)
    await env.service.recover_pending()
    assert (await task(env))['status'] == 'retry_wait' and not env.fresh.calls
    env.presence.read_legacy_presence.return_value = old
    env.now[0] += 31
    await env.service.recover_pending()
    assert (await task(env))['status'] == 'waiting_receipt'
