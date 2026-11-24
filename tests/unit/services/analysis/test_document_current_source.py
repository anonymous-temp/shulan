"""Real immutable Document store, synthetic interpretation and admission ports.

These tests prove offline provider behavior, not Profile admission or online AI.
"""

from copy import deepcopy
from dataclasses import replace

import pytest

from modules.analysis.basic_info_contracts import DocumentCurrentSourcePlan
from modules.analysis.basic_info_contracts import accepted_basic_info
from modules.analysis.complete_source_contracts import DocumentCompleteSourceUnavailable
from modules.analysis.current_understanding import DocumentCurrentBundle
from modules.analysis.snapshots import FACTS_CONTRACT_V2
from modules.analysis.snapshots import canonical_json
from tests.unit._support.current_document import SCOPE
from tests.unit._support.current_document import TEXT
from tests.unit._support.current_document import basic_fields
from tests.unit._support.current_document import capture
from tests.unit._support.current_document import environment
from tests.unit._support.current_document import no_value
from tests.unit._support.current_document import proof


def plan(bundle, handles, *, ref='current-plan', retired=()):
    return DocumentCurrentSourcePlan(
        bundle.material.scope,
        bundle.material.source_id,
        bundle.material.source_revision,
        tuple(handles),
        tuple(retired),
        ref,
        ref,
        canonical_json({'admissionKind': 'current', 'evidenceRef': ref}),
        ref,
        1,
    )


async def capture_source(env, handles, *, ref='current-plan', retired=()):
    env.admission.plans[ref] = plan(env.bundle, handles, ref=ref, retired=retired)
    return await env.source.capture_source(scope=SCOPE, verified_plan_ref=ref)


@pytest.mark.asyncio
async def test_current_bundle_retains_ai_grades_full_metadata_and_separate_acceptance():
    env = await environment(status='partial')
    facts = env.bundle.facts.payload
    assert isinstance(env.bundle, DocumentCurrentBundle)
    assert facts['clinicalAccepted'] is False
    assert [(f['field'], f['sourceReliability']) for f in facts['basicInfo']] == [
        ('name', 'medium'),
        ('gender', 'low'),
        ('age', 'medium'),
    ]
    assert facts['basicInfoMeta'] == env.raw['basicInfoMeta']
    assert env.bundle.understanding.payload['interpretation'] == env.raw
    assert env.bundle.facts.contract_version == FACTS_CONTRACT_V2
    env.port.prepare_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_basic_fields_and_reviewable_clinical_rows_publish_together():
    env = await environment(status='partial')
    handles = await env.source.capture_contributions(
        scope=SCOPE, understanding_snapshot_id=env.bundle.understanding.snapshot_id
    )
    receipt = await capture_source(env, handles)
    raw = await env.source.read_source_observations(scope=SCOPE, snapshot_id=receipt.source_snapshot_ref)
    assert len(raw['payload']['basicInfo']) == 3
    [clinical] = raw['payload']['clinicalContributions']
    [observation] = clinical['observations']
    assert observation['label'] == 'synthetic-test'
    assert observation['verificationStatus'] == 'unconfirmed'
    assert observation['sourceDocumentId'] == 'synthetic-doc'
    assert all(
        f['interpretationRound'] == 1 and f['understandingSnapshotRef'] == env.bundle.understanding.snapshot_id
        for f in raw['payload']['basicInfo']
    )
    assert {f['sourceReliability'] for f in raw['payload']['basicInfo']} == {'low', 'medium'}
    assert (await env.source.read_source_inventory(scope=SCOPE, snapshot_id=receipt.inventory_snapshot_ref))['payload'][
        'retiredContributions'
    ] == []


@pytest.mark.asyncio
async def test_complete_clinical_and_basic_fields_are_independent_fixed_contributions():
    env = await environment()
    handles = await env.source.capture_contributions(
        scope=SCOPE, understanding_snapshot_id=env.bundle.understanding.snapshot_id
    )
    assert len(handles) == 4
    pairs = [await env.source._handle(SCOPE, h) for h in handles]
    assert [s.payload['kind'] for s, _ in pairs] == ['clinical', 'basic_info', 'basic_info', 'basic_info']
    assert pairs[0][0].payload['basicInfo'] == []
    assert all(not s.payload['observations'] for s, _ in pairs[1:])
    receipt = await capture_source(env, handles)
    raw = await env.source.read_source_observations(scope=SCOPE, snapshot_id=receipt.source_snapshot_ref)
    assert raw['payload']['clinicalContributions'][0]['observations'] == [
        {**item, 'sourceDocumentId': env.bundle.material.payload['documentId']}
        for item in env.bundle.facts.payload['observations']
    ]


@pytest.mark.asyncio
async def test_not_found_ambiguous_and_conflicting_remain_archived_without_candidates_for_profile():
    def edit(raw):
        no_value(raw, 'name')
        no_value(raw, 'gender', 'unresolved')
        no_value(raw, 'age', 'conflicting')
        raw['basicInfoMeta']['age']['candidates'] = [
            {
                'value': 42,
                'sourceReliability': 'low',
                'evidence': proof('年龄：42岁'),
                'reason': '存在多个受检者',
                'subjectRef': '张三',
            }
        ]
        raw['sections'] = [basic_fields()]
        raw['observations'] = []

    env = await environment(status='partial', edit=edit)
    assert env.bundle.facts.payload['basicInfo'] == []
    assert env.bundle.facts.payload['basicInfoMeta']['age']['candidates']
    assert (
        await env.source.capture_contributions(
            scope=SCOPE, understanding_snapshot_id=env.bundle.understanding.snapshot_id
        )
        == ()
    )


@pytest.mark.parametrize(
    'change',
    [
        lambda raw: raw['basicInfoMeta']['name'].update(sourceReliability='very_high'),
        lambda raw: raw['basicInfoMeta']['name'].update(subjectStatus='ambiguous'),
        lambda raw: raw['basicInfoMeta']['name']['evidence'].update(start=1),
        lambda raw: raw['basicInfoMeta']['name']['evidence'].update(start=True),
        lambda raw: raw['basicInfoMeta']['name'].update(evidence=None),
        lambda raw: raw['basicInfoMeta']['name'].update(observedAt='not-a-date'),
        lambda raw: raw['basicInfo'].update(age=True),
        lambda raw: raw['basicInfo'].update(age=-1),
        lambda raw: raw['basicInfo'].update(age=151),
        lambda raw: raw['basicInfo'].update(age=float('nan')),
        lambda raw: raw['basicInfo'].update(gender='F'),
        lambda raw: raw['basicInfo'].update(name=''),
        lambda raw: raw['basicInfoMeta']['name'].update(status='not_found'),
    ],
)
def test_fixed_contract_rejects_wrong_grade_identity_evidence_dates_and_values(change):
    raw = basic_fields()
    change(raw)
    with pytest.raises(ValueError):
        accepted_basic_info(raw, TEXT)


@pytest.mark.asyncio
async def test_reads_and_lost_capture_response_recover_without_processing_or_latest_archive():
    env = await environment()
    handles = await env.source.capture_contributions(
        scope=SCOPE, understanding_snapshot_id=env.bundle.understanding.snapshot_id
    )
    receipt = await capture_source(env, handles)
    env.port.prepare_document.side_effect = AssertionError('no processing')
    env.port.read_prepared_round.side_effect = AssertionError('no live archive')
    assert (
        await env.source.capture_contributions(
            scope=SCOPE, understanding_snapshot_id=env.bundle.understanding.snapshot_id
        )
        == handles
    )
    assert await env.source.capture_source(scope=SCOPE, verified_plan_ref='current-plan') == receipt
    fixed = await env.api.read_bundle(scope=SCOPE, understanding_snapshot_id=env.bundle.understanding.snapshot_id)
    assert fixed == env.bundle
    assert (await env.source.read_source_observations(scope=SCOPE, snapshot_id=receipt.source_snapshot_ref))['payload'][
        'basicInfo'
    ]


@pytest.mark.asyncio
async def test_basic_only_change_carries_fields_and_keeps_exact_clinical_handle():
    env = await environment()
    original = await env.source.capture_contributions(
        scope=SCOPE, understanding_snapshot_id=env.bundle.understanding.snapshot_id
    )
    prior = plan(env.bundle, original)
    env.raw['basicInfoMeta']['name'].update(reason='后续轮次补充原文依据')
    env.document['interpretationResultRound'] = 2
    env.bundle = await capture(env)
    proposed = await env.source.propose_replacement(
        scope=SCOPE, prior=prior, understanding_snapshot_id=env.bundle.understanding.snapshot_id
    )
    assert all(h in proposed.active for h in original)
    assert len(proposed.active) == 7  # one old clinical, all old and new fields
    assert proposed.retired == () and proposed.evidence == []
    receipt = await capture_source(env, proposed.active)
    raw = await env.source.read_source_observations(scope=SCOPE, snapshot_id=receipt.source_snapshot_ref)
    assert len(raw['payload']['clinicalContributions']) == 1


@pytest.mark.asyncio
async def test_changed_clinical_retires_only_previous_clinical_and_carries_old_basic_fields():
    env = await environment()
    original = await env.source.capture_contributions(
        scope=SCOPE, understanding_snapshot_id=env.bundle.understanding.snapshot_id
    )
    prior = plan(env.bundle, original)
    env.raw['observations'][0]['resultValue'] = '88'
    env.document['interpretationResultRound'] = 2
    env.bundle = await capture(env)
    proposed = await env.source.propose_replacement(
        scope=SCOPE, prior=prior, understanding_snapshot_id=env.bundle.understanding.snapshot_id
    )
    assert original[0] not in proposed.active
    assert all(h in proposed.active for h in original[1:])
    assert [r.handle for r in proposed.retired] == [original[0]]
    assert proposed.evidence[0]['kind'] == 'fixed_clinical_input_superseded'
    assert len(proposed.active) == 7


@pytest.mark.asyncio
async def test_partial_recovery_keeps_previous_complete_clinical_contribution():
    env = await environment()
    original = await env.source.capture_contributions(
        scope=SCOPE, understanding_snapshot_id=env.bundle.understanding.snapshot_id
    )
    prior = plan(env.bundle, original)
    env.raw['processing']['status'] = 'partial'
    env.raw['observations'] = []
    env.document['interpretationResultRound'] = 2
    env.bundle = await capture(env)
    proposed = await env.source.propose_replacement(
        scope=SCOPE, prior=prior, understanding_snapshot_id=env.bundle.understanding.snapshot_id
    )
    assert original[0] in proposed.active and not proposed.retired


@pytest.mark.asyncio
async def test_forged_contribution_and_missing_real_admission_cannot_publish():
    env = await environment()
    handles = await env.source.capture_contributions(
        scope=SCOPE, understanding_snapshot_id=env.bundle.understanding.snapshot_id
    )
    snapshot, proof_snapshot = await env.source._handle(SCOPE, handles[1])
    payload = snapshot.payload
    payload['basicInfo'][0]['sourceReliability'] = 'very_high'
    forged = replace(snapshot, _payload_json=canonical_json(payload))
    await env.store.put(forged)
    with pytest.raises(DocumentCompleteSourceUnavailable, match='actual fixed'):
        await env.source._handle(SCOPE, replace(handles[1], snapshot_ref=forged.snapshot_id))
    env.source.admission = None
    with pytest.raises(DocumentCompleteSourceUnavailable, match='admission provider'):
        await env.source.capture_source(scope=SCOPE, verified_plan_ref='unverified')


@pytest.mark.asyncio
async def test_conflicting_clinical_inventory_is_rejected_before_source_is_accepted():
    env = await environment()
    old = await env.source.capture_contributions(
        scope=SCOPE, understanding_snapshot_id=env.bundle.understanding.snapshot_id
    )
    env.raw['observations'][0]['resultValue'] = '88'
    env.document['interpretationResultRound'] = 2
    env.bundle = await capture(env)
    new = await env.source.capture_contributions(
        scope=SCOPE, understanding_snapshot_id=env.bundle.understanding.snapshot_id
    )
    with pytest.raises(DocumentCompleteSourceUnavailable, match='conflicting active clinical'):
        await capture_source(env, (*old, *new))


@pytest.mark.asyncio
async def test_explicit_factories_preserve_the_injected_admission_boundary():
    from adapters.analysis.basic_info import create_document_basic_info_source_service
    from adapters.analysis.basic_info import create_document_basic_info_understanding_service

    env = await environment()
    api = create_document_basic_info_understanding_service(
        preparation=env.port, snapshots=env.store.snapshots, snapshot_chunks=env.store.chunks
    )
    assert (
        await api.read_bundle(scope=SCOPE, understanding_snapshot_id=env.bundle.understanding.snapshot_id) == env.bundle
    )
    source = create_document_basic_info_source_service(understanding=api, admission=env.admission)
    assert source.admission is env.admission
    assert await source.capture_contributions(
        scope=SCOPE, understanding_snapshot_id=env.bundle.understanding.snapshot_id
    )


def test_name_contract_uses_nonblank_text_without_an_unconfirmed_length_limit():
    raw = basic_fields()
    raw['basicInfo']['name'] = '名' * 513
    fields = accepted_basic_info(raw, TEXT)
    assert fields[0]['value'] == raw['basicInfo']['name']


def test_full_clinical_input_ignores_demographics_and_execution_but_keeps_clinical_text():
    from modules.analysis.basic_info_contracts import clinical_content_digest

    raw = {
        'reportType': 'lab',
        'reportTime': '2026-11-01',
        'summary': 'clinical original',
        'expansion': {'findings': 'original findings'},
        'unresolvedItems': [{'itemName': 'pending marker'}],
        'sections': [{'summary': 'section original', **basic_fields()}],
        **basic_fields(),
        'processing': {'status': 'complete', 'version': 'v12', 'sourceRevision': 1},
    }
    first = clinical_content_digest(raw, [])
    raw['basicInfo']['name'] = 'another name'
    raw['basicInfoMeta']['age']['reason'] = 'another basic field rationale'
    raw['sections'][0]['basicInfo']['age'] = 12
    raw['processing'].update(version='v13', sourceRevision=2)
    assert clinical_content_digest(raw, []) == first
    for container, field in ((raw, 'summary'), (raw['expansion'], 'findings'), (raw['sections'][0], 'summary')):
        saved = container[field]
        container[field] = 'changed clinical text'
        assert clinical_content_digest(raw, []) != first
        container[field] = saved


@pytest.mark.asyncio
async def test_current_fixed_round_capture_reads_public_archive_not_current_document():
    env = await environment()
    first = env.bundle
    env.document['interpretationResultRound'] = 2
    env.raw['basicInfo']['age'] = 43
    await capture(env)
    restored = await env.api.capture_round_bundle(scope=SCOPE, document_id=env.document['_id'], interpretation_round=1)
    assert restored == first
    assert restored.facts.payload['basicInfo'][2]['value'] == 42
    env.port.prepare_document.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['summary', 'section'])
async def test_current_clinical_description_change_retires_clinical_without_touching_basic(field):
    env = await environment()
    original = await env.source.capture_contributions(
        scope=SCOPE, understanding_snapshot_id=env.bundle.understanding.snapshot_id
    )
    prior = plan(env.bundle, original)
    if field == 'summary':
        env.raw['summary'] = 'changed clinical description'
    else:
        env.raw['sections'] = [{'summary': 'changed clinical subsection', 'observations': []}]
    env.document['interpretationResultRound'] = 2
    env.bundle = await capture(env)
    proposed = await env.source.propose_replacement(
        scope=SCOPE, prior=prior, understanding_snapshot_id=env.bundle.understanding.snapshot_id
    )
    assert {row.handle for row in proposed.retired} == {original[0]}
    assert set(original[1:]).issubset(proposed.active)
    assert original[0] not in proposed.active


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'quality',
    [None, [], 'complete', {}, {'status': None}, {'status': []}, {'status': 'unknown'}, {'status': False}, 'missing'],
)
async def test_current_invalid_or_missing_quality_keeps_basic_and_pending_clinical(quality):
    def edit(raw):
        if quality == 'missing':
            raw.pop('processing')
        else:
            raw['processing'] = deepcopy(quality)

    env = await environment(edit=edit)
    assert env.bundle.facts.payload['quality'] == {
        'status': 'unavailable',
        'reasonCode': 'document.clinical_quality_unavailable',
    }
    assert env.bundle.facts.payload['clinicalAccepted'] is False
    assert env.bundle.understanding.payload['interpretation'] == env.raw
    handles = await env.source.capture_contributions(
        scope=env.scope, understanding_snapshot_id=env.bundle.understanding.snapshot_id
    )
    assert len(handles) == 4
    pairs = [await env.source._handle(env.scope, handle) for handle in handles]
    clinical, *_ = pairs
    assert clinical[0].payload['observations'][0]['verificationStatus'] == 'unconfirmed'
    assert all(snapshot.payload['kind'] == 'basic_info' for snapshot, _ in pairs[1:])
    original = await env.api.read_bundle(
        scope=env.scope, understanding_snapshot_id=env.bundle.understanding.snapshot_id
    )
    assert original == env.bundle


@pytest.mark.asyncio
@pytest.mark.parametrize('status,accepted', [('complete', True), ('partial', False), ('unavailable', False)])
async def test_current_valid_original_quality_is_retained_for_independent_admission(status, accepted):
    env = await environment(status=status)
    assert env.bundle.facts.payload['quality'] == env.raw['processing']
    assert env.bundle.facts.payload['clinicalAccepted'] is accepted


@pytest.mark.asyncio
async def test_fixed_archive_rejects_both_forged_qualities_even_with_consistent_snapshot_refs():
    env = await environment(status='partial')
    facts_payload = env.bundle.facts.payload
    facts_payload.update(quality={'status': 'complete'}, clinicalAccepted=True)
    facts = replace(env.bundle.facts, _payload_json=canonical_json(facts_payload))
    understanding_payload = env.bundle.understanding.payload
    understanding_payload.update(quality={'status': 'complete'}, factsSnapshotRef=facts.snapshot_id)
    understanding = replace(env.bundle.understanding, _payload_json=canonical_json(understanding_payload))
    await env.store.put(facts)
    await env.store.put(understanding)
    with pytest.raises(ValueError, match='admission decisions'):
        await env.api.read_bundle(scope=env.scope, understanding_snapshot_id=understanding.snapshot_id)
    assert (
        await env.api.read_bundle(scope=env.scope, understanding_snapshot_id=env.bundle.understanding.snapshot_id)
        == env.bundle
    )


@pytest.mark.asyncio
async def test_partial_report_delivers_verified_and_both_pending_rows_from_the_same_document():
    def edit(raw):
        raw['sourceBindings'] = {'/observations/0': 'v6:source-original'}
        raw['observations'][0]['evidence'] = {'locator': {'table': 1, 'valueCell': 2}}
        raw['unresolvedItems'] = [
            {'itemName': 'reading without value', 'resultValue': None},
            {'itemName': 'reading without proof', 'resultValue': '4.4'},
        ]

    env = await environment(status='partial', edit=edit)
    handles = await env.source.capture_contributions(
        scope=SCOPE, understanding_snapshot_id=env.bundle.understanding.snapshot_id
    )
    receipt = await capture_source(env, handles)
    source = await env.source.read_source_observations(scope=SCOPE, snapshot_id=receipt.source_snapshot_ref)
    [clinical] = source['payload']['clinicalContributions']
    assert source['payload']['quality']['details']['profileRouting'] == 'report-patient.v1'
    assert [
        (row['observationRef'], row['rawValue'], row['verificationStatus']) for row in clinical['observations']
    ] == [
        ('/observations/0', '2.5', 'verified_source'),
        ('/unresolvedItems/0', None, 'unconfirmed'),
        ('/unresolvedItems/1', '4.4', 'unconfirmed'),
    ]
    assert {row['sourceDocumentId'] for row in clinical['observations']} == {'synthetic-doc'}


@pytest.mark.asyncio
async def test_sections_with_distinct_supported_people_keep_separate_fields_and_unassigned_rows():
    text = TEXT + '姓名：李四\n性别：男\n'

    def edit(raw):
        for field in ('name', 'gender', 'age'):
            no_value(raw, field)
        first = basic_fields()
        second = basic_fields()
        no_value(second, 'age')
        for field, value, quote in (('name', '李四', '姓名：李四'), ('gender', '男', '性别：男')):
            second['basicInfo'][field] = value
            start = text.index(quote)
            second['basicInfoMeta'][field].update(
                subjectRef=None,
                subjectEvidence=None,
                evidence={'quote': quote, 'start': start, 'end': start + len(quote)},
            )
        first['observations'] = [{'itemName': 'A', 'resultValue': '4'}]
        second['observations'] = [{'itemName': 'B', 'resultValue': '5'}]
        raw['sections'] = [first, second, {'observations': [{'itemName': 'C', 'resultValue': '6'}]}]

    env = await environment(status='partial', text=text, edit=edit)
    handles = await env.source.capture_contributions(
        scope=SCOPE, understanding_snapshot_id=env.bundle.understanding.snapshot_id
    )
    receipt = await capture_source(env, handles)
    source = await env.source.read_source_observations(scope=SCOPE, snapshot_id=receipt.source_snapshot_ref)
    [clinical] = source['payload']['clinicalContributions']
    slots = {row['value']: row['patientSlot'] for row in source['payload']['basicInfo'] if row['field'] == 'name'}
    assert slots['张三'] != slots['李四']
    assert {row['label']: row['patientSlot'] for row in clinical['observations']} == {
        'synthetic-test': 'unassigned',
        'A': slots['张三'],
        'B': slots['李四'],
        'C': 'unassigned',
    }


@pytest.mark.asyncio
async def test_independent_root_and_section_patients_carry_their_own_report_slots():
    text = TEXT + '姓名：李四\n'

    def edit(raw):
        child = basic_fields()
        no_value(child, 'gender')
        no_value(child, 'age')
        child['basicInfo']['name'] = '李四'
        start = text.index('姓名：李四')
        child['basicInfoMeta']['name'].update(
            subjectRef=None,
            subjectEvidence=None,
            evidence={'quote': '姓名：李四', 'start': start, 'end': start + len('姓名：李四')},
        )
        child['observations'] = [{'itemName': 'child observation', 'resultValue': '3'}]
        raw['_profileSourcePatient'] = basic_fields()
        no_value(raw, 'name', 'conflicting')
        raw['sections'] = [child]

    env = await environment(status='partial', text=text, edit=edit)
    observations = env.bundle.facts.payload['observations']
    named = {
        row['value']: row['patientSlot'] for row in env.bundle.facts.payload['basicInfo'] if row['field'] == 'name'
    }
    assert observations[0]['patientSlot'] == named['张三']
    assert observations[1]['patientSlot'] == named['李四']
    assert named['张三'] != named['李四']
