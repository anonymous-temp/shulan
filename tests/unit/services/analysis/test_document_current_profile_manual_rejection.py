"""Manual rejection survives Document inventory and current source correction."""

import pytest

from health_profile.models import FactStatus
from health_profile.service import HealthProfileService
from tests.unit._support.current_document import capture
from tests.unit._support.current_document import no_value
from tests.unit._support.current_document_profile import clinical_facts
from tests.unit._support.current_document_profile import composition
from tests.unit._support.current_document_profile import info
from tests.unit._support.current_document_profile import managed
from tests.unit._support.current_document_profile import publish
from tests.unit._support.current_document_profile import settle


@pytest.mark.asyncio
@pytest.mark.parametrize('other_fallback', [False, True])
async def test_explicit_retirement_does_not_reactivate_a_user_rejected_report_fact(monkeypatch, other_fallback):
    def low_grade(raw):
        raw['basicInfoMeta']['age']['sourceReliability'] = 'low'

    flow = await composition(monkeypatch, edit=low_grade)
    await publish(flow)
    low = (await info(flow)).fields['age']
    assert low.source_reliability == 'low'
    clinical = await clinical_facts(flow)
    assert len(clinical) == 1

    for field in ('name', 'gender'):
        no_value(flow.env.raw, field)
    flow.env.raw['processing']['status'] = 'partial'
    flow.env.raw['basicInfoMeta']['age']['sourceReliability'] = 'medium'
    flow.env.document['interpretationResultRound'] = 2
    newer = await capture(flow.env)
    addition = await flow.runtime.managed.execute(managed(flow, understanding=newer.understanding.snapshot_id))
    await settle(flow)

    user_id, subject_id = flow.scope.user_id, flow.scope.subject_id
    if other_fallback:
        low_source = next(row for row in (await info(flow)).sources if row.fact_id == low.fact_id)
        evidence = {
            key: value for key, value in low_source.source_ref.items() if key not in ('sourceId', 'sourceRevision')
        }
        evidence['value'] = 41
        await flow.profile.basic_info.ingest_report(
            user_id,
            subject_id,
            source_id='synthetic-independent-report',
            source_revision=1,
            publication_revision=1,
            fields=[evidence],
        )

    before = await info(flow)
    selected = before.fields['age']
    assert selected.source_reliability == 'medium' and selected.fact_id != low.fact_id
    original_fact = await flow.repository.get_fact(user_id, subject_id, low.fact_id)
    view = await flow.repository.get_view(user_id, subject_id)
    profile_service = HealthProfileService(flow.repository, clock=lambda: flow.now[0])
    await profile_service.resolve_fact(
        user_id,
        subject_id,
        low.fact_id,
        'reject',
        expected_fact_version=original_fact.version,
        expected_profile_version=view.profile_version,
        request_id='reject-low',
    )
    rejected_fact = await flow.repository.get_fact(user_id, subject_id, low.fact_id)
    assert rejected_fact.status is FactStatus.RETRACTED
    assert (await info(flow)).fields['age'].fact_id == selected.fact_id

    command = managed(
        flow,
        retire=(selected.source_ref['contributionId'],),
        request='retire-medium-with-rejected-low',
        prior=2,
        source=addition['sourceSnapshotRef'],
    )
    receipt = await flow.runtime.managed.execute(command)
    await settle(flow)
    ledger = await flow.ledger.read_source(flow.scope, command.source_id)
    assert ledger['publicationRevision'] == ledger['appliedRevision'] == ledger['settledRevision'] == 3
    assert await flow.runtime.managed.execute(command) == receipt
    current = await info(flow)
    rejected = next(row for row in current.sources if row.fact_id == low.fact_id)
    assert not rejected.active and not rejected.current and not rejected.deleted
    assert rejected.value == low.value
    assert await flow.repository.get_fact(user_id, subject_id, low.fact_id) == rejected_fact
    assert current.fields['age'].value == (41 if other_fallback else None)
    assert current.fields['name'] == before.fields['name']
    assert current.fields['gender'] == before.fields['gender']
    assert await clinical_facts(flow) == clinical
