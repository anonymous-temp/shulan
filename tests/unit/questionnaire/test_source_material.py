"""Exact immutable chunk reconstruction, scope checks and large accepted JSON evidence."""

import hashlib
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from bson import BSON

from modules.health_profile.contracts import ProfileScope
from modules.health_profile.contracts import ProfileSourceSnapshotRefV1
from modules.questionnaire.profile_source import QuestionnaireSourceConflict
from modules.questionnaire.profile_source import saved_binding
from tests.unit.questionnaire.source_fakes import environment
from tests.unit.questionnaire.source_fakes import save
from tests.unit.questionnaire.source_fakes import submission


@pytest.mark.asyncio
async def test_reader_is_constructible_before_worker_and_restores_original_survey_order():
    env = environment()
    assert not env.owned.documents and not env.source.documents and not env.owned.indexes
    await save(env)
    scope = {'userId': 'u', 'subjectId': 'self'}
    material = await env.service.materials.prepare(saved_binding(env.raw), scope=scope)
    read = env.resources.snapshot_reader.read_source_snapshot
    snapshot = await read(
        ProfileSourceSnapshotRefV1.model_validate(material['event']['snapshotRef']),
        scope=ProfileScope.model_validate(scope),
    )
    assert [row['questionId'] for row in snapshot['answers']] == ['z_note', 'a_choice']
    assert snapshot['answers'][0]['answer'] == '原始文字'
    assert snapshot['submissionProof'] == material['submissionProof']
    assert not env.owned.indexes
    with pytest.raises(PermissionError):
        await read(
            ProfileSourceSnapshotRefV1.model_validate(material['event']['snapshotRef']),
            scope=ProfileScope(user_id='other', subject_id='self'),
        )


@pytest.mark.asyncio
async def test_material_larger_than_mongo_document_limit_uses_small_bson_chunks():
    env = environment()
    env.raw = submission(label='\x01' * 3_000_000)
    assert len(BSON.encode(env.raw)) < 15 * 1024 * 1024
    await save(env)
    material = await env.service.materials.prepare(saved_binding(env.raw), scope={'userId': 'u', 'subjectId': 'self'})
    reference = material['snapshotReference']
    assert reference['byteLength'] > 16 * 1024 * 1024 and len(reference['chunkDigests']) > 32
    assert max(len(BSON.encode(row)) for row in env.owned.documents.values()) < 600_000
    result = await env.resources.snapshot_reader.read_source_snapshot(
        ProfileSourceSnapshotRefV1.model_validate(material['event']['snapshotRef']),
        scope=ProfileScope(user_id='u', subject_id='self'),
    )
    assert result['answers'][1]['answer'] == env.raw['questionnaire']['pages'][0]['elements'][1]['choices'][0]['text']


@pytest.mark.asyncio
async def test_interrupted_chunks_never_expose_manifest_and_replay_restores_exact_bytes():
    env = environment()
    store = env.resources.store
    value = {'raw': 'x' * 1_100_000}
    original = store.immutable
    count = [0]

    async def interrupted(row):
        count[0] += 1
        if count[0] == 2:
            raise RuntimeError('process interrupted')
        return await original(row)

    store.immutable = interrupted
    with pytest.raises(RuntimeError):
        await store.put_blob(value)
    assert not any(row.get('recordKind') == 'material_manifest' for row in env.owned.documents.values())
    store.immutable = original
    ref = await store.put_blob(value)
    assert await store.read_blob(ref) == value
    ref2 = await store.put_blob(deepcopy(value))
    assert ref2 == ref
    env.owned.documents[ref['blobId'] + ':1']['content'] = b'z' * (512 * 1024)
    with pytest.raises(QuestionnaireSourceConflict):
        await store.read_blob(ref)


@pytest.mark.asyncio
async def test_blob_freezes_caller_before_first_await_and_manifest_ref_is_exact():
    env = environment()
    value = {'raw': 'original'}
    original = env.resources.store.immutable

    async def mutate(row):
        value['raw'] = 'changed'
        return await original(row)

    env.resources.store.immutable = mutate
    ref = await env.resources.store.put_blob(value)
    assert await env.resources.store.read_blob(ref) == {'raw': 'original'}
    missing = deepcopy(ref)
    missing['chunkDigests'][0] = hashlib.sha256(b'other').hexdigest()
    with pytest.raises(LookupError):
        await env.resources.store.read_blob(missing)
    env.resources.store.insert = AsyncMock(side_effect=AssertionError('pure reader must not write'))
    assert await env.resources.store.read_blob(ref) == {'raw': 'original'}
