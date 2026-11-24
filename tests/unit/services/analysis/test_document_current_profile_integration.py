"""Actual merged Profile intake with Document transport and synthetic Mongo I/O."""

from copy import deepcopy
from datetime import timedelta

import pytest

from adapters.analysis.basic_info import create_document_basic_info_profile_runtime
from adapters.analysis.basic_info import create_document_basic_info_source_readers
from adapters.analysis.document_trace import MongoDocumentTraceStore
from health_profile.basic_info_contracts import BasicInfoValuesV1
from health_profile.contracts import ProfileSourceEventV1
from health_profile.contracts import profile_content_digest
from health_profile.source_publications import publication_id
from health_profile.source_publications import source_key
from modules.analysis.application.analysis_outcomes import empty_analysis
from modules.analysis.application.current_interpreter import CurrentHealthReportInterpreter
from modules.analysis.application.documents import DocumentInterpretationError
from modules.analysis.application.extraction import merge_document_results
from modules.analysis.basic_info_contracts import clinical_content
from modules.analysis.basic_info_managed import DocumentCurrentManagedCommand
from modules.analysis.preparation import payload_digest
from modules.analysis.snapshots import digest
from tests.unit._support import current_document as source_fixtures
from tests.unit._support.current_document import REPORT_TEXT
from tests.unit._support.current_document import UPLOAD
from tests.unit._support.current_document import basic_fields
from tests.unit._support.current_document import capture
from tests.unit._support.current_document import model_report
from tests.unit._support.current_document import no_value
from tests.unit._support.current_document_profile import actual_analysis_composition
from tests.unit._support.current_document_profile import clinical_facts
from tests.unit._support.current_document_profile import composition
from tests.unit._support.current_document_profile import cycle
from tests.unit._support.current_document_profile import info
from tests.unit._support.current_document_profile import install_profile
from tests.unit._support.current_document_profile import managed
from tests.unit._support.current_document_profile import publish
from tests.unit._support.current_document_profile import settle
from tests.unit._support.native_report_pdf import native_pdf


@pytest.mark.asyncio
async def test_current_transport_accepts_without_a_cutover_authorization(monkeypatch):
    flow = await composition(monkeypatch)
    result = await flow.runtime.publisher.publish(
        scope=flow.scope,
        snapshot_id=flow.capture.source_snapshot_ref,
        request_id='without-cutover',
        expected_prior_revision=None,
    )
    assert result.state == 'received'
    assert await flow.profile.worker.recover_pending() == 1
    assert (await info(flow)).fields['name'].value


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['complete', 'partial'])
async def test_actual_factories_publish_document_source_into_current_profile(monkeypatch, status):
    flow = await composition(monkeypatch, status=status)
    assert flow.runtime.readers is flow.readers
    assert flow.runtime.publisher.source_reader is flow.readers.source
    assert flow.runtime.source.admission is flow.readers.plans
    await publish(flow)
    current = await info(flow)
    assert current.fields['name'].value and current.fields['name'].source_reliability == 'medium'
    assert current.fields['gender'].source_reliability == 'low'
    assert current.fields['age'].value == 42
    rows = await clinical_facts(flow)
    assert [(item.display_name, item.status, item.verification_status) for item in rows] == [
        ('ALT', 'pending', 'unconfirmed'),
    ]
    state = await flow.ledger.read_source(flow.scope, flow.env.bundle.material.source_id)
    assert state['appliedRevision'] == state['publicationRevision'] == 1
    flow.env.port.prepare_document.assert_not_awaited()
    assert flow.env.port.read_prepared_round.await_count == 1


@pytest.mark.asyncio
async def test_real_profile_highest_confirmation_survives_report_and_withdrawal(monkeypatch):
    flow = await composition(monkeypatch)
    registered = await flow.profile.basic_info.register(
        flow.scope.user_id,
        'api-name',
        values=BasicInfoValuesV1(name='confirmed-client-name', age=42, gender='女'),
        confirmed_at=flow.now[0],
        subject_id=flow.scope.subject_id,
    )
    await flow.profile.basic_info.apply(flow.scope.user_id, registered.command_id)
    await publish(flow)
    assert (await info(flow)).fields['name'].value == 'confirmed-client-name'
    await flow.runtime.publisher.retract(
        scope=flow.scope,
        source_id=flow.env.bundle.material.source_id,
        source_revision=1,
        request_id='withdraw',
        expected_prior_revision=1,
        lifecycle_epoch=flow.epoch,
    )
    await settle(flow)
    assert (await info(flow)).fields['name'].value == 'confirmed-client-name'
    assert (await info(flow)).fields['name'].source_reliability == 'very_high'
    assert not await clinical_facts(flow)


@pytest.mark.asyncio
async def test_partial_basic_round_preserves_actual_complete_clinical_fact(monkeypatch):
    flow = await composition(monkeypatch)
    await publish(flow)
    clinical = tuple(row.model_dump_json() for row in await clinical_facts(flow))
    flow.env.raw['processing']['status'] = 'partial'
    flow.env.raw['basicInfoMeta']['age']['sourceReliability'] = 'low'
    flow.env.document['interpretationResultRound'] = 2
    current = await capture(flow.env)
    command = managed(flow, understanding=current.understanding.snapshot_id)
    result = await flow.runtime.managed.execute(command)
    await settle(flow)
    assert result['publicationRevision'] == 2
    assert tuple(row.model_dump_json() for row in await clinical_facts(flow)) == clinical
    assert (await info(flow)).fields['age'].source_reliability == 'medium'
    source = await flow.readers.source.read_source_observations(
        scope=flow.scope, snapshot_id=result['sourceSnapshotRef']
    )
    assert source['payload']['clinicalAvailable']
    assert len([row for row in source['payload']['basicInfo'] if row['field'] == 'age']) == 2


def test_clinical_projection_rebases_only_structural_links_and_keeps_independent_content():
    proof = {'quote': 'ALT 12', 'locator': {'sourceSection': 1, 'valueCell': 'A2'}}
    item = {'itemName': 'ALT', 'resultValue': '12', 'sourceSection': 1, 'evidence': proof}
    tree = {
        'reportType': 'imaging',
        'expansion': {'findings': 'independent finding'},
        'expansionEvidence': {'findings': {'quote': 'independent finding'}},
        'unknownClinicalContent': {'sourceSection': 1},
        'sourceBindings': {
            '/sections/1/observations/0': 'v6:native-source',
            '/sections/1/observations/not-an-index': 'unrecognized',
        },
        'sections': [
            {**empty_analysis(), **basic_fields(), 'sectionIndex': 0},
            {**empty_analysis(), 'observations': [item], 'sectionIndex': 1},
        ],
    }
    rows = [
        {'observationRef': '/sections/1/observations/0', 'evidence': [proof]},
        {'observationRef': '/sections/1/observations/not-an-index', 'evidence': [proof]},
    ]
    original = deepcopy((tree, rows))
    projected = clinical_content(tree, rows)
    clinical = projected['interpretation']
    assert clinical['reportType'] == 'imaging'
    assert clinical['expansion'] == tree['expansion']
    assert clinical['expansionEvidence'] == tree['expansionEvidence']
    assert clinical['unknownClinicalContent'] == {'sourceSection': 1}
    assert clinical['sourceBindings'] == {
        '/sections/0/observations/0': 'v6:native-source',
        '/sections/1/observations/not-an-index': 'unrecognized',
    }
    assert len(clinical['sections']) == 1
    assert clinical['sections'][0]['sectionIndex'] == 0
    assert clinical['sections'][0]['observations'] == [{**item, 'sourceSection': 0}]
    assert projected['observations'] == [{**rows[0], 'observationRef': '/sections/0/observations/0'}, rows[1]]
    assert (tree, rows) == original

    for field, value in (
        ('title', 'original title'),
        ('unresolvedItems', [{'itemName': 'unknown test'}]),
        ('extraClinicalField', None),
    ):
        with_clinical_content = deepcopy(tree)
        with_clinical_content['sections'][0][field] = value
        retained = clinical_content(with_clinical_content, rows)['interpretation']['sections']
        assert len(retained) == 2 and retained[0][field] == value


@pytest.mark.asyncio
async def test_demographic_section_changes_preserve_clinical_facts_and_fixed_provenance(monkeypatch):
    def merged_round(raw, *, with_age=False, with_name=False, proof_changed=False):
        clinical = [
            dict(
                empty_analysis(),
                reportType='lab',
                status='normal',
                reportTime='2026-11-01',
                observations=[{'itemName': name, 'resultValue': '12', 'unit': 'U/L'}],
            )
            for name in ('ALT', 'AST')
        ]
        clinical[0]['observations'][0]['evidence'] = {
            'quote': 'ALT 12',
            'locator': {'table': 'table-1', 'valueCell': 'A2' if not proof_changed else 'A3'},
        }
        container = dict(empty_analysis(), reportType='checkup', sections=clinical[:1])
        container['basicInfo'] = deepcopy(raw['basicInfo'])
        container['basicInfoMeta'] = deepcopy(raw['basicInfoMeta'])
        for field in ('name', 'gender', 'age'):
            if field != 'age' or not with_age:
                no_value(container, field)
        if with_name:
            clinical[0]['basicInfo'] = deepcopy(raw['basicInfo'])
            clinical[0]['basicInfoMeta'] = deepcopy(raw['basicInfoMeta'])
            for field in ('gender', 'age'):
                no_value(clinical[0], field)
        return dict(merge_document_results([container, clinical[1]]), processing={'status': 'complete'})

    def initialize(raw):
        original = merged_round(raw, with_age=False)
        raw.clear()
        raw.update(original)

    flow = await composition(monkeypatch, edit=initialize)
    initial = flow.env.bundle
    await publish(flow)
    initial_rows = await clinical_facts(flow)
    assert len(initial_rows) == 2
    initial_ids = {row.fact_id for row in initial_rows}
    source = await flow.readers.source.read_source_observations(
        scope=flow.scope, snapshot_id=flow.capture.source_snapshot_ref
    )
    initial_clinical = source['payload']['clinicalContributions']
    prior_source = flow.capture.source_snapshot_ref
    assert (await info(flow)).fields['age'].value is None

    rounds = ({'with_age': True}, {'with_name': True}, {'with_name': True, 'proof_changed': True})
    for number, changes in enumerate(rounds, 2):
        flow.env.raw = merged_round(basic_fields(), **changes)
        flow.env.document['interpretationResultRound'] = number
        current = await capture(flow.env)
        result = await flow.runtime.managed.execute(
            managed(
                flow,
                understanding=current.understanding.snapshot_id,
                request=f'demographic-section-{number}',
                prior=number - 1,
                source=prior_source,
            )
        )
        await settle(flow)
        prior_source = result['sourceSnapshotRef']
        assert result['publicationRevision'] == number
        live = await flow.ledger.read_source(flow.scope, initial.material.source_id)
        assert live['appliedRevision'] == live['settledRevision'] == number
        source = await flow.readers.source.read_source_observations(scope=flow.scope, snapshot_id=prior_source)
        clinical = source['payload']['clinicalContributions']
        fact_ids = {row.fact_id for row in await clinical_facts(flow)}
        refs = [row['observationRef'] for row in current.facts.payload['observations']]
        offset = int(changes.get('with_age', False))
        assert refs == [f'/sections/{index}/observations/0' for index in (offset, offset + 1)]
        assert current.understanding.payload['interpretation'] == flow.env.raw
        if not changes.get('proof_changed'):
            assert current.facts.payload['clinicalContentDigest'] == initial.facts.payload['clinicalContentDigest']
            assert clinical == initial_clinical
            assert fact_ids == initial_ids
            assert (await info(flow)).fields['age'].value == 42
        else:
            assert current.facts.payload['clinicalContentDigest'] != initial.facts.payload['clinicalContentDigest']
            assert clinical != initial_clinical
            assert clinical[0]['observations'][0]['evidence'][0]['locator']['valueCell'] == 'A3'
            assert len(fact_ids) == 2 and fact_ids.isdisjoint(initial_ids)

    fixed = await flow.env.api.capture_round_bundle(
        scope=flow.scope, document_id=initial.material.payload['documentId'], interpretation_round=1
    )
    assert fixed.understanding == initial.understanding
    assert fixed.facts == initial.facts


@pytest.mark.asyncio
async def test_explicit_basic_retirement_does_not_withdraw_actual_clinical_fact(monkeypatch):
    flow = await composition(monkeypatch)
    await publish(flow)
    clinical = tuple(row.model_dump_json() for row in await clinical_facts(flow))
    source = await flow.readers.source.read_source_observations(
        scope=flow.scope, snapshot_id=flow.capture.source_snapshot_ref
    )
    selected = [row['contributionId'] for row in source['payload']['contributions'] if row['kind'] == 'basic_info']
    await flow.runtime.managed.execute(managed(flow, retire=selected))
    await settle(flow)
    assert all(field.value is None for field in (await info(flow)).fields.values())
    assert tuple(row.model_dump_json() for row in await clinical_facts(flow)) == clinical


@pytest.mark.asyncio
async def test_partial_retirement_reselects_active_lower_grade_and_allows_next_document_correction(monkeypatch):
    corrected_quote = '年龄：43岁'
    monkeypatch.setattr(source_fixtures, 'TEXT', source_fixtures.TEXT + corrected_quote + '\n')
    flow = await composition(monkeypatch)
    await publish(flow)
    initial = await info(flow)
    initial_age = initial.fields['age']
    assert (initial_age.value, initial_age.source_reliability) == (42, 'medium')
    clinical = await clinical_facts(flow)
    assert len(clinical) == 1

    for field in ('name', 'gender'):
        no_value(flow.env.raw, field)
    flow.env.raw['processing']['status'] = 'partial'
    flow.env.raw['basicInfoMeta']['age']['sourceReliability'] = 'low'
    flow.env.document['interpretationResultRound'] = 2
    lower = await capture(flow.env)
    addition = await flow.runtime.managed.execute(managed(flow, understanding=lower.understanding.snapshot_id))
    await settle(flow)
    before = await info(flow)
    retained = next(row for row in before.sources if row.field == 'age' and row.source_reliability == 'low')
    assert retained.active and not retained.current and retained.value == 42
    assert before.fields['age'].fact_id == initial_age.fact_id

    retired_id = initial_age.source_ref['contributionId']
    retirement = managed(
        flow, retire=(retired_id,), request='retire-medium-age', prior=2, source=addition['sourceSnapshotRef']
    )
    result = await flow.runtime.managed.execute(retirement)
    assert result['publication']['state'] == 'received'
    await settle(flow)
    ledger = await flow.ledger.read_source(flow.scope, retirement.source_id)
    assert ledger['publicationRevision'] == ledger['appliedRevision'] == ledger['settledRevision'] == 3
    reserved = await flow.ledger.read_reserved(flow.scope, retirement.source_id, retirement.command_id)
    assert reserved['receipt']['state'] == 'applied'

    current = await info(flow)
    assert (current.fields['age'].value, current.fields['age'].source_reliability) == (42, 'low')
    assert current.fields['age'].fact_id == retained.fact_id
    assert current.fields['age'].source_ref == retained.source_ref
    assert not next(row for row in current.sources if row.fact_id == initial_age.fact_id).active
    assert current.fields['name'] == initial.fields['name']
    assert current.fields['gender'] == initial.fields['gender']
    assert await clinical_facts(flow) == clinical

    flow.env.raw['basicInfo']['age'] = 43
    flow.env.raw['basicInfoMeta']['age'] = source_fixtures.extracted(43, corrected_quote, 'medium')['meta']
    flow.env.document['interpretationResultRound'] = 3
    corrected = await capture(flow.env)
    correction = managed(
        flow,
        understanding=corrected.understanding.snapshot_id,
        request='correct-age',
        prior=3,
        source=result['sourceSnapshotRef'],
    )
    await flow.runtime.managed.execute(correction)
    await settle(flow)
    settled = await flow.ledger.read_source(flow.scope, retirement.source_id)
    assert settled['publicationRevision'] == settled['appliedRevision'] == settled['settledRevision'] == 4

    assert await flow.runtime.managed.execute(retirement) == result
    assert await flow.ledger.read_source(flow.scope, retirement.source_id) == settled
    updated = await info(flow)
    assert (updated.fields['age'].value, updated.fields['age'].source_reliability) == (43, 'medium')
    assert not next(row for row in updated.sources if row.fact_id == initial_age.fact_id).active
    assert updated.fields['name'] == initial.fields['name']
    assert updated.fields['gender'] == initial.fields['gender']
    assert await clinical_facts(flow) == clinical


async def legacy_retirement_plan(monkeypatch, *, remove_medium=True):
    flow = await composition(monkeypatch)
    await publish(flow)
    initial = await info(flow)
    initial_age = initial.fields['age']
    assert initial_age.source_reliability == 'medium'

    for field in ('name', 'gender'):
        no_value(flow.env.raw, field)
    flow.env.raw['processing']['status'] = 'partial'
    flow.env.raw['basicInfoMeta']['age']['sourceReliability'] = 'low'
    flow.env.document['interpretationResultRound'] = 2
    lower = await capture(flow.env)
    addition = await flow.runtime.managed.execute(managed(flow, understanding=lower.understanding.snapshot_id))
    await settle(flow)
    before = await info(flow)
    retained = next(row for row in before.sources if row.field == 'age' and row.source_reliability == 'low')
    retiree = initial_age if remove_medium else retained
    retirement = managed(
        flow,
        retire=(retiree.source_ref['contributionId'],),
        request='retire-from-fixed-plan',
        prior=2,
        source=addition['sourceSnapshotRef'],
    )
    result = await flow.runtime.managed.execute(retirement)
    intent = await flow.ledger.read_reserved(flow.scope, retirement.source_id, retirement.command_id)
    event = ProfileSourceEventV1.model_validate(intent['event'])
    inbox = flow.profile.inbox
    publication = await inbox.store.get(publication_id(event))
    cursor_id = 'cursor_' + source_key(event.scope, event.source_type, event.source_id)
    cursor = await inbox.claim(await inbox.store.get(cursor_id))
    try:
        subject = await flow.repository.get_subject(flow.scope.user_id, flow.scope.subject_id)
        plan = await flow.profile.worker.applier._plan(event, subject, cursor, publication)
        assert plan['documentBasicInfo'].pop('retiredBasicContributions')
        # This is the frozen plan shape saved before the R4 retirement proof was supported.
        await inbox.save_plan(publication['_id'], plan)
    finally:
        await inbox.release(cursor)
    return flow, retirement, result, event, plan, retained, intent['_id']


@pytest.mark.asyncio
@pytest.mark.parametrize('previous_failure', [False, True])
async def test_retirement_reopens_legacy_fixed_plan_after_restart_or_redrive(monkeypatch, previous_failure):
    flow, retirement, result, event, plan, retained, intent_id = await legacy_retirement_plan(monkeypatch)
    clinical = await clinical_facts(flow)
    if previous_failure:
        await flow.profile.inbox.mark_processing(publication_id(event))
        await flow.profile.inbox.fail(
            publication_id(event), 'basic_info.unqualified_source_replacement', max_attempts=1
        )
        flow.now[0] += timedelta(minutes=1)
        await flow.runtime.recovery.recover_pending()
        prior = await flow.ledger.read_intent(flow.scope, intent_id)
        assert prior['recoveryState'] == 'failed'

    install_profile(flow)
    if previous_failure:
        await flow.profile.inbox.redrive(event.event_id, scope=event.scope)
    assert await flow.profile.worker.recover_pending() == 1
    if previous_failure:
        await flow.runtime.recovery.redrive(scope=flow.scope, intent_id=intent_id, reason='fixed retirement proof')
    flow.now[0] += timedelta(minutes=1)
    await flow.runtime.recovery.recover_pending()

    ledger = await flow.ledger.read_source(flow.scope, retirement.source_id)
    assert ledger['publicationRevision'] == ledger['appliedRevision'] == ledger['settledRevision'] == 3
    assert (await info(flow)).fields['age'].fact_id == retained.fact_id
    assert await clinical_facts(flow) == clinical
    publication = await flow.profile.inbox.store.get(publication_id(event))
    assert publication['plan'] == plan
    (proof,) = publication['basicInfoRetirementRecovery']['retirements']
    assert proof['contributionId'] == retirement.retire_contribution_ids[0]
    assert publication['basicInfoApplication']['actionDigest'] == profile_content_digest(plan['documentBasicInfo'])
    assert await flow.runtime.managed.execute(retirement) == result

    flow.env.document['interpretationResultRound'] = 3
    next_round = await capture(flow.env)
    await flow.runtime.managed.execute(
        managed(
            flow,
            understanding=next_round.understanding.snapshot_id,
            request='after-recovered-retirement',
            prior=3,
            source=result['sourceSnapshotRef'],
        )
    )
    await settle(flow)
    ledger = await flow.ledger.read_source(flow.scope, retirement.source_id)
    assert ledger['publicationRevision'] == ledger['appliedRevision'] == ledger['settledRevision'] == 4
    assert await clinical_facts(flow) == clinical


@pytest.mark.asyncio
@pytest.mark.parametrize('ack_loss', ['proof', 'legacy-commit', 'recovered-commit'])
async def test_retirement_recovery_keeps_original_plan_and_receipt_after_ack_loss(monkeypatch, ack_loss):
    flow, _, _, event, plan, retained, _ = await legacy_retirement_plan(
        monkeypatch, remove_medium=ack_loss != 'legacy-commit'
    )
    clinical = await clinical_facts(flow)
    if ack_loss == 'proof':
        inbox = flow.profile.inbox
        save = inbox.save_document_retirement_recovery

        async def lose_proof_ack(*args):
            await save(*args)
            raise OSError('synthetic retirement proof acknowledgement loss')

        with monkeypatch.context() as lost:
            lost.setattr(inbox, 'save_document_retirement_recovery', lose_proof_ack)
            assert await flow.profile.worker.recover_pending() == 0
        assert (await info(flow)).fields['age'].source_reliability == 'medium'
    elif ack_loss == 'legacy-commit':
        action = plan['documentBasicInfo']
        await flow.profile.basic_info.ingest_report(
            event.scope.user_id,
            event.scope.subject_id,
            source_id=action['sourceId'],
            source_revision=action['sourceRevision'],
            fields=action['fields'],
            publication_revision=action['publicationRevision'],
            lifecycle_epoch=action['lifecycleEpoch'],
        )
        assert (await info(flow)).fields['age'].source_reliability == 'medium'
    else:
        basic = flow.profile.basic_info
        apply = basic._resume_fixed_report_action

        async def lose_commit_ack(*args, **kwargs):
            await apply(*args, **kwargs)
            raise OSError('synthetic recovered retirement acknowledgement loss')

        with monkeypatch.context() as lost:
            lost.setattr(basic, '_resume_fixed_report_action', lose_commit_ack)
            assert await flow.profile.worker.recover_pending() == 0
        assert (await info(flow)).fields['age'].fact_id == retained.fact_id
    before = await flow.profile.inbox.store.get(publication_id(event))
    assert before.get('basicInfoApplication') is None
    assert ('basicInfoRetirementRecovery' in before) == (ack_loss != 'legacy-commit')

    install_profile(flow)
    resolver = flow.profile.inbox.current_document_source

    async def read_must_not_recur(*args, **kwargs):
        pytest.fail('frozen proof or previously committed action must not reread Document')

    monkeypatch.setattr(resolver, 'resolve_fixed', read_must_not_recur)
    flow.now[0] += timedelta(minutes=1)
    assert await flow.profile.worker.recover_pending() == 1
    after = await flow.profile.inbox.store.get(publication_id(event))
    assert before.get('basicInfoRetirementRecovery') == after.get('basicInfoRetirementRecovery')
    assert after['plan'] == plan
    assert after['basicInfoApplication']['actionDigest'] == profile_content_digest(plan['documentBasicInfo'])
    assert (await info(flow)).fields['age'].source_reliability == ('medium' if ack_loss == 'legacy-commit' else 'low')
    assert await clinical_facts(flow) == clinical
    receipt = await flow.profile.inbox.get_receipt(event.event_id, scope=event.scope)
    assert receipt.status == 'applied' and receipt.watermark.settled_publication_revision == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['plan-fields', 'snapshot', 'lifecycle', 'lease', 'retraction'])
async def test_retirement_recovery_rejects_drift_and_respects_whole_source_retraction(monkeypatch, fault):
    flow, retirement, _, event, plan, _, _ = await legacy_retirement_plan(monkeypatch)
    inbox = flow.profile.inbox
    resolver = inbox.current_document_source
    resolve = resolver.resolve_fixed
    publication_key = publication_id(event)
    cursor_key = 'cursor_' + source_key(event.scope, event.source_type, event.source_id)
    clinical = await clinical_facts(flow)
    before = {key: row.model_dump(exclude={'as_of'}) for key, row in (await info(flow)).fields.items()}

    async def drift(*args, **kwargs):
        material = await resolve(*args, **kwargs)
        if fault == 'plan-fields':
            publication = await inbox.store.get(publication_key)
            next(field for field in publication['plan']['documentBasicInfo']['fields'] if field['field'] == 'age')[
                'value'
            ] = 42.0
            assert await inbox.store.replace(publication, publication['revision']) is not None
        elif fault == 'snapshot':
            material.snapshot['sourceRevision'] += 1
        elif fault == 'lifecycle':
            await cycle(flow)
        elif fault == 'lease':
            flow.now[0] += timedelta(minutes=3)
        else:
            await flow.runtime.publisher.retract(
                scope=flow.scope,
                source_id=retirement.source_id,
                source_revision=event.source_revision,
                request_id='withdraw-during-proof-validation',
                expected_prior_revision=3,
                lifecycle_epoch=flow.epoch,
            )
        return material

    monkeypatch.setattr(resolver, 'resolve_fixed', drift)
    applied = await flow.profile.worker.recover_pending()
    publication = await inbox.store.get(publication_key)
    cursor = await inbox.store.get(cursor_key)
    assert 'basicInfoRetirementRecovery' not in publication
    if fault == 'retraction':
        assert applied == 1
        assert publication['basicInfoApplication']['effect'] == 'withdraw'
        assert publication['basicInfoApplication']['publicationRevision'] == 4
        assert (await info(flow)).fields['age'].value is None
        assert cursor['acceptedRetractionRevision'] == 4
        monkeypatch.setattr(resolver, 'resolve_fixed', resolve)
        await settle(flow)
        final = await flow.ledger.read_source(flow.scope, retirement.source_id)
        assert final['publicationRevision'] == final['appliedRevision'] == final['settledRevision'] == 4
        return
    assert applied == 0
    assert publication.get('basicInfoApplication') is None
    assert cursor['appliedPublicationRevision'] == cursor['settledPublicationRevision'] == 2
    if fault != 'lifecycle':
        after = {key: row.model_dump(exclude={'as_of'}) for key, row in (await info(flow)).fields.items()}
        assert after == before
        assert await clinical_facts(flow) == clinical
    if fault == 'lease':
        assert publication['status'] == 'processing'
        assert publication['plan'] == plan
        monkeypatch.setattr(resolver, 'resolve_fixed', resolve)
        assert await flow.profile.worker.recover_pending() == 1
    else:
        assert publication['status'] == 'failed'
        assert (
            publication['errorCode']
            == {
                'plan-fields': 'source.basic_info_plan_conflict',
                'snapshot': 'source.content_digest_mismatch',
                'lifecycle': 'source.document_lifecycle_mismatch',
            }[fault]
        )


@pytest.mark.asyncio
async def test_receive_ack_loss_and_restart_reuses_actual_profile_publication(monkeypatch):
    flow = await composition(monkeypatch)
    receive = flow.profile.inbox.receive

    async def lose_ack(*args, **kwargs):
        await receive(*args, **kwargs)
        raise OSError('synthetic accepted Profile ACK loss')

    monkeypatch.setattr(flow.profile.inbox, 'receive', lose_ack)
    with pytest.raises(OSError, match='ACK loss'):
        await flow.runtime.publisher.publish(
            scope=flow.scope,
            snapshot_id=flow.capture.source_snapshot_ref,
            request_id='publication-first',
            expected_prior_revision=None,
        )
    flow.env.port.prepare_document.side_effect = AssertionError('restart must not execute Analysis')
    flow.env.port.read_prepared_round.side_effect = AssertionError('restart must not use mutable Analysis archives')
    flow.readers = create_document_basic_info_source_readers(
        prepared=flow.prepared, ledger=flow.ledger, admission_records=flow.records
    )
    install_profile(flow)
    flow.runtime = create_document_basic_info_profile_runtime(
        readers=flow.readers, profile_inbox=flow.profile.inbox, clock=lambda: flow.now[0]
    )
    assert await flow.profile.worker.recover_pending() == 1
    fixed_info = await info(flow)
    fixed = fixed_info.model_dump_json()
    flow.now[0] += timedelta(minutes=5)
    result = await flow.runtime.recovery.recover_pending()
    assert result.processed == 1 and not result.deferred
    assert (
        await flow.profile.basic_info.read(flow.scope.user_id, subject_id=flow.scope.subject_id, as_of=fixed_info.as_of)
    ).model_dump_json() == fixed
    assert len(await clinical_facts(flow)) == 1
    assert len(flow.ledger.intents.documents) == 1
    assert len([row for row in flow.cols['publications'].documents if row['recordKind'] == 'publication']) == 1
    assert (await flow.ledger.read_source(flow.scope, flow.env.bundle.material.source_id))['appliedRevision'] == 1


@pytest.mark.asyncio
async def test_lost_basic_commit_ack_and_later_api_confirmation_survive_real_worker_retry(monkeypatch):
    flow = await composition(monkeypatch)
    ingest = flow.profile.basic_info.ingest_report
    attempts = []

    async def lose_first_ack(*args, **kwargs):
        result = await ingest(*args, **kwargs)
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError('synthetic applied basic ACK loss')
        return result

    monkeypatch.setattr(flow.profile.basic_info, 'ingest_report', lose_first_ack)
    await flow.runtime.publisher.publish(
        scope=flow.scope,
        snapshot_id=flow.capture.source_snapshot_ref,
        request_id='publication-first',
        expected_prior_revision=None,
    )
    assert await flow.profile.worker.recover_pending() == 0
    confirmation = await flow.profile.basic_info.register(
        flow.scope.user_id,
        'later-client',
        values=BasicInfoValuesV1(name='later-client-name', age=42, gender='女'),
        confirmed_at=flow.now[0],
        subject_id=flow.scope.subject_id,
    )
    await flow.profile.basic_info.apply(flow.scope.user_id, confirmation.command_id)
    flow.now[0] += timedelta(minutes=5)
    assert await flow.profile.worker.recover_pending() == 1
    await flow.runtime.recovery.recover_pending()
    assert len(attempts) == 2
    assert (await info(flow)).fields['name'].value == 'later-client-name'
    assert (await info(flow)).fields['name'].source_reliability == 'very_high'
    assert len(await clinical_facts(flow)) == 1


@pytest.mark.asyncio
async def test_accepted_old_epoch_cannot_write_after_reactivation_and_fixed_doc_snapshot_stays_original(monkeypatch):
    flow = await composition(monkeypatch)
    receipt = await flow.runtime.publisher.publish(
        scope=flow.scope,
        snapshot_id=flow.capture.source_snapshot_ref,
        request_id='publication-first',
        expected_prior_revision=None,
    )
    original = await flow.readers.source.read_source_observations(
        scope=flow.scope, snapshot_id=flow.capture.source_snapshot_ref
    )
    await cycle(flow)
    assert await flow.profile.worker.recover_pending() == 0
    profile_receipt = await flow.profile.inbox.get_receipt(
        next(iter(flow.ledger.intents.documents.values()))['event']['eventId'],
        scope=await flow.profile.reader.resolve_scope(flow.scope.user_id, subject_id=flow.scope.subject_id),
    )
    assert profile_receipt.status == 'failed'
    assert profile_receipt.error_code == 'source.document_lifecycle_mismatch'
    flow.now[0] += timedelta(minutes=5)
    await flow.runtime.recovery.recover_pending()
    current = await info(flow)
    assert current.lifecycle_epoch > flow.epoch and all(field.value is None for field in current.fields.values())
    assert not await clinical_facts(flow)
    assert (
        await flow.readers.source.read_source_observations(
            scope=flow.scope, snapshot_id=flow.capture.source_snapshot_ref
        )
        == original
    )
    assert original['payload']['coverage']['lifecycleEpoch'] == flow.epoch
    assert receipt.state == 'received'


@pytest.mark.asyncio
async def test_retraction_arrives_before_first_publish_and_late_publish_cannot_revive_profile(monkeypatch):
    flow = await composition(monkeypatch)
    source, proof = await flow.readers.source.read_publication_material(
        scope=flow.scope, snapshot_id=flow.capture.source_snapshot_ref
    )
    event = {
        'scope': flow.scope.as_dict(),
        'sourceType': 'health_report',
        'sourceId': source['sourceId'],
        'sourceRevision': source['sourceRevision'],
        'eventKind': 'publish',
        'contentDigest': digest(source),
        'snapshotRef': {
            'provider': 'document',
            'contractVersion': source['contractVersion'],
            'snapshotId': flow.capture.source_snapshot_ref,
        },
    }
    first = await flow.ledger.reserve(flow.scope, event, 'publication-first', None, current_proof=proof)
    receipt = await flow.runtime.publisher.retract(
        scope=flow.scope,
        source_id=source['sourceId'],
        source_revision=1,
        request_id='retract-before-delivery',
        expected_prior_revision=1,
        lifecycle_epoch=flow.epoch,
    )
    assert receipt.state == 'waiting'
    assert await flow.profile.worker.recover_pending() == 0
    assert all(field.value is None for field in (await info(flow)).fields.values())
    late = await flow.runtime.recovery.receiver.deliver(first['event'])
    assert late.state == 'received'
    assert await flow.profile.worker.recover_pending() == 1
    assert all(field.value is None for field in (await info(flow)).fields.values())
    assert not await clinical_facts(flow)
    assert await flow.profile.worker.recover_pending() == 1
    flow.now[0] += timedelta(minutes=5)
    await flow.runtime.recovery.recover_pending()
    assert all(field.value is None for field in (await info(flow)).fields.values())
    assert not await clinical_facts(flow)
    ledger = await flow.ledger.read_source(flow.scope, source['sourceId'])
    assert ledger['appliedRetractionRevision'] == ledger['settledRevision'] == 2


@pytest.mark.asyncio
async def test_public_profile_snapshot_and_source_reads_remain_pure_after_provider_restart(monkeypatch):
    flow = await composition(monkeypatch)
    await publish(flow)
    before = deepcopy(flow.cols['facts'].documents)
    flow.env.port.prepare_document.side_effect = AssertionError('read must not execute')
    flow.env.port.read_prepared_round.side_effect = AssertionError('read must use Document fixed artifacts')
    flow.readers = create_document_basic_info_source_readers(
        prepared=flow.prepared, ledger=flow.ledger, admission_records=flow.records
    )
    install_profile(flow)
    profile_scope = await flow.profile.reader.resolve_scope(flow.scope.user_id, subject_id=flow.scope.subject_id)
    value = await flow.readers.profile.read_source_observations(
        scope=profile_scope, snapshot_id=flow.capture.source_snapshot_ref
    )
    snapshot = await flow.profile.reader.read_v2(flow.scope.user_id, subject_id=flow.scope.subject_id)
    assert snapshot.basic_info.fields['age'].value == 42
    assert value['payload']['coverage']['lifecycleEpoch'] == snapshot.basic_info.lifecycle_epoch == flow.epoch
    assert flow.cols['facts'].documents == before
    assert not hasattr(flow.readers.profile.source, 'capture_source')
    from modules.analysis.application.documents import DocumentNotFoundError
    from modules.health_profile.contracts import ProfileScope

    with pytest.raises(DocumentNotFoundError):
        await flow.readers.profile.read_source_observations(
            scope=ProfileScope(user_id=flow.scope.user_id, subject_id='unrelated-subject'),
            snapshot_id=flow.capture.source_snapshot_ref,
        )
    assert len(flow.cols['subjects'].documents) == 1


@pytest.mark.asyncio
async def test_current_retraction_reader_requires_the_original_fixed_source_epoch(monkeypatch):
    from modules.analysis.complete_source_contracts import DocumentCompleteSourceUnavailable
    from modules.analysis.snapshots import RETRACTION_CONTRACT
    from modules.analysis.snapshots import DocumentSnapshot
    from modules.analysis.snapshots import canonical_json

    flow = await composition(monkeypatch)
    forged = DocumentSnapshot(
        flow.scope,
        RETRACTION_CONTRACT,
        flow.env.bundle.material.source_id,
        1,
        canonical_json(
            {'publicationRevision': 2, 'priorRevision': 1, 'eventKind': 'retract', 'lifecycleEpoch': flow.epoch + 2}
        ),
    )
    await flow.readers.source.snapshots.put(forged)
    profile_scope = await flow.profile.reader.resolve_scope(flow.scope.user_id, subject_id=flow.scope.subject_id)
    with pytest.raises(DocumentCompleteSourceUnavailable, match='original'):
        await flow.readers.profile.read_retraction(scope=profile_scope, snapshot_id=forged.snapshot_id)


@pytest.mark.asyncio
async def test_actual_profile_accepts_valid_basic_and_reviewable_observations_from_missing_quality(monkeypatch):
    flow = await composition(monkeypatch, edit=lambda raw: raw.pop('processing'))
    await publish(flow)
    assert (await info(flow)).fields['age'].value == 42
    [clinical] = await clinical_facts(flow)
    assert (clinical.display_name, clinical.status) == ('ALT', 'pending')
    source = await flow.readers.source.read_source_observations(
        scope=flow.scope, snapshot_id=flow.capture.source_snapshot_ref
    )
    assert source['payload']['clinicalAvailable'] is True
    assert len(source['payload']['clinicalContributions']) == 1
    assert source['payload']['quality']['details']['originalQualities'] == [None, None, None, None]


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid_quality', ['missing', None, {'status': 'unknown'}])
async def test_missing_or_invalid_quality_on_new_basic_round_does_not_withdraw_real_clinical(
    monkeypatch, invalid_quality
):
    flow = await composition(monkeypatch)
    await publish(flow)
    previous = tuple(row.model_dump_json() for row in await clinical_facts(flow))
    if invalid_quality == 'missing':
        flow.env.raw.pop('processing')
    else:
        flow.env.raw['processing'] = invalid_quality
    flow.env.document['interpretationResultRound'] = 2
    next_round = await capture(flow.env)
    result = await flow.runtime.managed.execute(
        managed(flow, understanding=next_round.understanding.snapshot_id, request='missing-quality-basic')
    )
    await settle(flow)
    assert tuple(row.model_dump_json() for row in await clinical_facts(flow)) == previous
    assert (await info(flow)).fields['age'].value == 42
    source = await flow.readers.source.read_source_observations(
        scope=flow.scope, snapshot_id=result['sourceSnapshotRef']
    )
    assert source['payload']['clinicalAvailable'] is True
    assert len(source['payload']['clinicalContributions']) == 1


async def publish_prepared(flow, bundle):
    plan_ref = await flow.runtime.admission.prepare_source_plan(
        scope=flow.scope,
        prepared=flow.prepared.input(bundle),
        request_id='first-source',
        lifecycle_epoch=flow.epoch,
    )
    flow.capture = await flow.runtime.source.capture_source(scope=flow.scope, verified_plan_ref=plan_ref)
    await publish(flow)
    return await flow.readers.source.read_source_observations(
        scope=flow.scope, snapshot_id=flow.capture.source_snapshot_ref
    )


async def analysis_fixed(flow, bundle):
    return await flow.analysis.read_prepared_round(
        user_id=flow.scope.user_id,
        subject_id=flow.scope.subject_id,
        document_id=bundle.receipt.document_id,
        interpretation_round=bundle.facts.payload['interpretationRound'],
    )


@pytest.mark.asyncio
async def test_current_analysis_port_prepares_and_reuses_full_document_then_publishes_to_profile(monkeypatch):
    times = {
        'name': ('20260901T0915+0800', '2026-09-01T09:15+08:00'),
        'gender': ('20260901T091506.1234567+0800', '2026-09-01T09:15:06.1234567+08:00'),
        'age': ('20260901T091500+0860', None),
    }
    model = model_report()
    text = REPORT_TEXT
    for field, label in (('name', '姓名'), ('gender', '性别'), ('age', '年龄')):
        raw_time, _ = times[field]
        model['basicInfoMeta'][field]['observedAt'] = raw_time
        text += f'{label}观察时间：{raw_time}\n'
    flow = await actual_analysis_composition(monkeypatch, [model], text=text)
    bundle = await flow.prepared.prepare_bundle(scope=flow.scope, **UPLOAD)
    fixed = await analysis_fixed(flow, bundle)
    assert bundle.material.payload['parsedDocument'] == fixed.parsed_document
    assert fixed.parsed_document['text'] == text
    assert bundle.material.payload['parseContentDigest'] == payload_digest(fixed.parsed_document)
    assert bundle.understanding.payload['interpretation'] == fixed.interpretation
    assert bundle.understanding.payload['interpretationContentDigest'] == payload_digest(fixed.interpretation)
    assert flow.prepared.input(bundle).selection == fixed.output_selection
    assert bundle.facts.payload['extractorRevision'] == fixed.preparation_binding.input_snapshot.interpretation_revision
    assert bundle.facts.payload['validationRevision'] == fixed.preparation_binding.input_snapshot.validation_revision
    assert not bundle.receipt.reused
    assert not any(field.value is not None for field in (await info(flow)).fields.values())
    assert not flow.ledger.intents.documents
    saved = deepcopy((flow.snapshots.documents, flow.snapshot_chunks.documents))

    duplicate = await flow.prepared.prepare_bundle(scope=flow.scope, document_id=bundle.receipt.document_id)
    assert duplicate.receipt.reused and duplicate.receipt.trace_id != bundle.receipt.trace_id
    assert (duplicate.material, duplicate.facts, duplicate.understanding) == (
        bundle.material,
        bundle.facts,
        bundle.understanding,
    )
    assert (flow.snapshots.documents, flow.snapshot_chunks.documents) == saved
    analysis_stores = (flow.traces, flow.trace_chunks, flow.upstream.index_collection)
    before_read = deepcopy(tuple(store.documents for store in analysis_stores))
    assert await analysis_fixed(flow, duplicate) == fixed
    retained = await flow.prepared.validate_prepared(scope=flow.scope, prepared=flow.prepared.input(duplicate))
    assert retained.understanding == bundle.understanding
    assert tuple(store.documents for store in analysis_stores) == before_read
    assert (flow.snapshots.documents, flow.snapshot_chunks.documents) == saved
    source = await publish_prepared(flow, bundle)
    values = (await info(flow)).fields
    assert {key: value.value for key, value in values.items()} == {'name': '测试甲', 'gender': 'female', 'age': 42}
    assert {key: value.source_reliability for key, value in values.items()} == {
        'name': 'medium',
        'gender': 'low',
        'age': 'medium',
    }
    clinical = await clinical_facts(flow)
    assert len(clinical) == 1 and clinical[0].display_name == 'ALT'
    assert len(source['payload']['clinicalContributions']) == 1
    contributions = {item['field']: item for item in source['payload']['basicInfo']}
    assert len(contributions) == len(source['payload']['basicInfo']) == 3
    assert bundle.facts.payload['basicInfoMeta'] == fixed.interpretation['basicInfoMeta']
    for field, (_, expected) in times.items():
        metadata = fixed.interpretation['basicInfoMeta'][field]
        contribution = contributions[field]
        assert metadata['observedAt'] == contribution['observedAt'] == values[field].observed_at == expected
        assert values[field].source_ref['sourceId'] == bundle.material.source_id
        assert values[field].source_ref['contributionId'] == contribution['contributionId']
        for key in ('evidence', 'subjectEvidence'):
            quote = metadata[key]
            assert text[quote['start'] : quote['end']] == quote['quote']
            assert quote == contribution[key] == values[field].source_ref[key]
    assert (await flow.ledger.read_source(flow.scope, bundle.material.source_id))['appliedRevision'] == 1
    assert len(flow.gateway.complete_requests) == len(flow.upstream.client.submit_calls) == 1
    assert len(flow.upstream.client.result_calls) == flow.upstream.downloader.download.await_count == 1
    assert flow.gateway.complete_requests[0].profile.name == CurrentHealthReportInterpreter.CAPABILITY
    flow.upstream.source.save.assert_not_awaited()


@pytest.mark.asyncio
async def test_resumed_unfixed_analysis_result_publishes_observation_time_to_profile(monkeypatch):
    raw_time = '20260901T091506.1234567+0800'
    expected = '2026-09-01T09:15:06.1234567+08:00'
    model = model_report()
    model['basicInfoMeta']['age']['observedAt'] = raw_time
    flow = await actual_analysis_composition(monkeypatch, [model], text=REPORT_TEXT + f'年龄观察时间：{raw_time}\n')

    async def interrupt_archive(*args):
        raise OSError('synthetic interruption before fixing Analysis round')

    with monkeypatch.context() as interrupted:
        interrupted.setattr(flow.upstream.index, 'put_archived_round', interrupt_archive)
        with pytest.raises(OSError, match='synthetic interruption before fixing Analysis round'):
            await flow.prepared.prepare_bundle(scope=flow.scope, **UPLOAD)
    document = next(iter(flow.upstream.index_collection.documents.values()))
    document_id, round_number = document['_id'], document['interpretationRound']
    assert document['interpretationResultRound'] == round_number
    assert document['interpretationValidatedVersion'] == CurrentHealthReportInterpreter.VERSION
    assert not flow.snapshots.documents and not flow.ledger.intents.documents
    assert (
        await flow.upstream.index.get_archived_round(
            document_id, flow.scope.user_id, flow.scope.subject_id, round_number
        )
        is None
    )

    # Recreate the persisted, validated payload produced before the upstream date fix.
    traces = MongoDocumentTraceStore(flow.traces, flow.trace_chunks)
    old_ref = document['payloadRefs']['interpretation']
    prior = await traces.read_payload(old_ref, flow.scope.user_id, flow.scope.subject_id, 'interpretation')
    assert prior['basicInfoMeta']['age']['observedAt'] == expected
    prior['basicInfoMeta']['age']['observedAt'] = raw_time
    await traces.write_payload(old_ref, flow.scope.user_id, 'interpretation', prior)

    bundle = await flow.prepared.prepare_bundle(scope=flow.scope, document_id=document_id)
    fixed = await analysis_fixed(flow, bundle)
    assert fixed.preparation_binding.input_snapshot.interpretation_round == round_number
    assert fixed.preparation_binding.input_snapshot.interpretation_artifact_ref != old_ref
    prior['basicInfoMeta']['age']['observedAt'] = expected
    assert fixed.interpretation == prior == bundle.understanding.payload['interpretation']
    assert bundle.understanding.payload['interpretationContentDigest'] == payload_digest(prior)

    source = await publish_prepared(flow, bundle)
    age = (await info(flow)).fields['age']
    contribution = next(item for item in source['payload']['basicInfo'] if item['field'] == 'age')
    assert age.value == contribution['value'] == 42 and age.source_reliability == 'medium'
    assert age.observed_at == contribution['observedAt'] == expected
    assert age.source_ref['evidence'] == contribution['evidence'] == prior['basicInfoMeta']['age']['evidence']
    assert len(await clinical_facts(flow)) == 1
    assert len(flow.gateway.complete_requests) == len(flow.upstream.client.submit_calls) == 1
    assert len(flow.upstream.client.result_calls) == flow.upstream.downloader.download.await_count == 1


@pytest.mark.asyncio
async def test_retryable_analysis_round_promotes_clinical_without_rewriting_previous_document_bundle(monkeypatch):
    flow = await actual_analysis_composition(
        monkeypatch, [model_report(incomplete=True), model_report(incomplete=True), model_report()]
    )
    first = await flow.prepared.prepare_bundle(scope=flow.scope, **UPLOAD)
    first_fixed = await analysis_fixed(flow, first)
    assert first.facts.payload['quality']['status'] == 'partial'
    assert first.facts.payload['quality']['retryable'] is True
    assert first.facts.payload['clinicalAccepted'] is False
    source = await publish_prepared(flow, first)
    assert len(source['payload']['clinicalContributions']) == 1
    assert (await info(flow)).fields['age'].value == 42
    [pending] = await clinical_facts(flow)
    assert (pending.display_name, pending.status) == ('ALT', 'pending')

    recovered = await flow.prepared.prepare_bundle(scope=flow.scope, document_id=first.receipt.document_id)
    assert recovered.material.source_id == first.material.source_id
    for key in ('contentSha256', 'parseArtifactRef', 'parseContentDigest', 'originalArtifactRef'):
        assert recovered.material.payload[key] == first.material.payload[key]
    assert recovered.understanding.snapshot_id != first.understanding.snapshot_id
    assert recovered.facts.payload['interpretationRound'] == first.facts.payload['interpretationRound'] + 1
    assert recovered.facts.payload['clinicalAccepted'] is True
    assert await analysis_fixed(flow, first) == first_fixed
    retained = await flow.prepared.validate_prepared(scope=flow.scope, prepared=flow.prepared.input(first))
    assert retained.understanding == first.understanding
    command = DocumentCurrentManagedCommand(
        flow.scope,
        first.material.source_id,
        'completed-round',
        flow.epoch,
        1,
        flow.capture.source_snapshot_ref,
        understanding_snapshot_ref=recovered.understanding.snapshot_id,
    )
    receipt = await flow.runtime.managed.execute(command)
    await settle(flow)
    assert receipt['publicationRevision'] == 2
    clinical = await clinical_facts(flow)
    assert len(clinical) == 1 and clinical[0].display_name == 'ALT'
    assert (await info(flow)).fields['age'].value == 42
    assert len(flow.gateway.complete_requests) == 3
    assert len(flow.upstream.client.submit_calls) == flow.upstream.downloader.download.await_count == 1


@pytest.mark.asyncio
async def test_merged_analysis_name_candidates_are_archived_without_blocking_other_contributions(monkeypatch):
    text = REPORT_TEXT + ''.join(f'候选姓名：候选{i:03d}\n' for i in range(101))
    first, checked = model_report(), model_report()
    for raw, indices in ((first, range(60)), (checked, range(60, 101))):
        raw['basicInfo']['name'] = None
        raw['basicInfoMeta']['name'].update(
            status='conflicting',
            sourceReliability=None,
            evidence=None,
            candidates=[
                {
                    'value': f'候选{i:03d}',
                    'evidence': {'quote': f'候选姓名：候选{i:03d}'},
                    'sourceReliability': 'low',
                    'reason': '原文有多个候选。',
                    'subjectRef': '测试甲',
                }
                for i in indices
            ],
        )
    flow = await actual_analysis_composition(monkeypatch, [first, checked], text=text)
    bundle = await flow.prepared.prepare_bundle(scope=flow.scope, **UPLOAD)
    fixed = await analysis_fixed(flow, bundle)
    candidates = bundle.facts.payload['basicInfoMeta']['name']['candidates']
    assert candidates == fixed.interpretation['basicInfoMeta']['name']['candidates']
    assert {row['value'] for row in candidates} == {f'候选{i:03d}' for i in range(101)}
    assert all(
        text[row['evidence']['start'] : row['evidence']['end']] == row['evidence']['quote'] for row in candidates
    )
    assert bundle.facts.payload['clinicalAccepted'] is True
    source = await publish_prepared(flow, bundle)
    assert {row['field'] for row in source['payload']['basicInfo']} == {'gender', 'age'}
    values = (await info(flow)).fields
    assert values['name'].value is None
    assert values['gender'].value == 'female' and values['age'].value == 42
    assert len(await clinical_facts(flow)) == 1
    assert len(flow.gateway.complete_requests) == 2


@pytest.mark.asyncio
async def test_unreadable_analysis_round_remains_archived_without_document_or_profile_contributions(monkeypatch):
    flow = await actual_analysis_composition(monkeypatch, [], text='')
    with pytest.raises(DocumentInterpretationError) as failure:
        await flow.prepared.prepare_bundle(scope=flow.scope, **UPLOAD)
    assert failure.value.result['processing']['contentOutcome'] == 'no_readable_text'
    document = next(iter(flow.upstream.index_collection.documents.values()))
    fixed = await flow.analysis.read_prepared_round(
        user_id=flow.scope.user_id,
        subject_id=flow.scope.subject_id,
        document_id=document['_id'],
        interpretation_round=document['interpretationResultRound'],
    )
    assert fixed.parsed_document['text'] == ''
    assert fixed.interpretation['processing']['status'] == 'unavailable'
    with pytest.raises(ValueError, match='invalid fixed basic information'):
        await flow.prepared.understanding.capture_round_bundle(
            scope=flow.scope,
            document_id=fixed.document_id,
            interpretation_round=fixed.preparation_binding.input_snapshot.interpretation_round,
        )
    assert not flow.snapshots.documents and not flow.snapshot_chunks.documents and not flow.ledger.intents.documents
    assert not any(field.value is not None for field in (await info(flow)).fields.values())
    assert not await clinical_facts(flow)
    assert not flow.gateway.complete_requests
    assert len(flow.upstream.client.submit_calls) == flow.upstream.downloader.download.await_count == 1


@pytest.mark.asyncio
async def test_current_native_pdf_proofs_reach_profile_as_ordered_clinical_evidence(monkeypatch):
    text = REPORT_TEXT + '肠道菌群检测报告\n肠道菌群多样性 7.36\n肠道微生物健康指数 -0.02\n'
    raw = model_report()
    raw['reportType'] = 'microbiome'
    flow = await actual_analysis_composition(monkeypatch, [raw], text=text, native_content=native_pdf())
    bundle = await flow.prepared.prepare_bundle(scope=flow.scope, **UPLOAD)
    fixed = await analysis_fixed(flow, bundle)
    original = fixed.interpretation['observations']
    native = {row['itemName']: row for row in original if isinstance(row.get('evidence'), dict)}
    assert {key: row['resultValue'] for key, row in native.items()} == {
        '肠道菌群多样性': '7.36',
        '肠道微生物健康指数': '-0.02',
    }
    assert bundle.understanding.payload['interpretation']['observations'] == original
    source = await publish_prepared(flow, bundle)
    published = {row['label']: row for row in source['payload']['clinicalContributions'][0]['observations']}
    for label, row in native.items():
        assert published[label]['evidence'] == [row['evidence']]
        assert published[label]['rawValue'] == row['resultValue']
    assert published['ALT']['evidence'] == []
    assert {row.display_name for row in await clinical_facts(flow)} == {'ALT', *native}
    assert (await info(flow)).fields['age'].value == 42
    assert len(flow.gateway.complete_requests) == 1


@pytest.mark.asyncio
async def test_new_partial_round_preserves_prior_verified_observation_and_adds_pending(monkeypatch):
    def initial(raw):
        raw['sourceBindings'] = {'/observations/0': 'v6:original-lab-cell'}
        raw['observations'][0]['evidence'] = {'locator': {'table': 1, 'valueCell': 2}}

    flow = await composition(monkeypatch, edit=initial)
    await publish(flow)
    [first] = await clinical_facts(flow)
    assert (first.display_name, first.status, first.verification_status) == ('ALT', 'active', 'verified_source')

    flow.env.raw['processing']['status'] = 'partial'
    flow.env.raw['observations'] = []
    flow.env.raw['sourceBindings'] = {}
    flow.env.raw['unresolvedItems'] = [{'itemName': 'AST', 'resultValue': None}]
    flow.env.document['interpretationResultRound'] = 2
    new = await capture(flow.env)
    result = await flow.runtime.managed.execute(managed(flow, understanding=new.understanding.snapshot_id))
    await settle(flow)
    snapshot = await flow.readers.source.read_source_observations(
        scope=flow.scope, snapshot_id=result['sourceSnapshotRef']
    )
    assert result['publicationRevision'] == 2
    assert len(snapshot['payload']['clinicalContributions']) == 2
    facts = {fact.display_name: fact for fact in await clinical_facts(flow)}
    assert set(facts) == {'ALT', 'AST'}
    assert facts['ALT'].value == first.value and facts['ALT'].verification_status == 'verified_source'
    assert facts['AST'].status == 'pending' and facts['AST'].value is None
