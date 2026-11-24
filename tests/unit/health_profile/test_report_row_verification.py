"""Exercise the Profile mapping policy for fixed current report observations."""

from datetime import UTC
from datetime import datetime
from types import SimpleNamespace

from health_profile.contracts import ProfileScope
from health_profile.ingestion import HealthFactPolicy
from health_profile.models import SubjectKind
from health_profile.source_material import ProfileSourceMaterial
from health_profile.source_material import ProfileSourceMaterialMapper


def test_report_observation_verification_is_independent_of_report_quality():
    scope = ProfileScope(user_id='synthetic-user', subject_id='synthetic-subject')
    event = SimpleNamespace(
        source_type='health_report',
        scope=scope,
        source_id='synthetic-source',
        source_revision=1,
        content_digest='a' * 64,
        occurred_at=datetime(2026, 11, 15, tzinfo=UTC),
    )
    items = (
        ('ALT', '24', '2026-11-12', 'verified_source'),
        ('AST', None, '2026-11-12', 'unconfirmed'),
        ('肌酐', '31', None, 'verified_source'),
        ('血糖', '4.8', '2026-11-12', 'conflicted'),
    )
    rows = tuple(
        {
            'observationRef': f'/observations/{index}',
            'label': name,
            'rawValue': value,
            'observedAt': at,
            'verificationStatus': status,
        }
        for index, (name, value, at, status) in enumerate(items)
    )
    material = ProfileSourceMaterial(scope, 'health_report', event.source_id, 1, {}, rows, report_time='2026-11-11')
    candidates, mapping = ProfileSourceMaterialMapper().map(event, material)
    policy = HealthFactPolicy()
    decisions = tuple(policy.evaluate(row, subject_kind=SubjectKind.SELF) for row in candidates)

    assert mapping.skipped_item_count == 0
    assert tuple(row.fact_status for row in decisions) == ('active', 'pending', 'pending', 'disputed')
    assert tuple(row.verification_status for row in decisions) == (
        'verified_source',
        'unconfirmed',
        'unconfirmed',
        'conflicted',
    )
    assert candidates[1].value is None
    assert candidates[2].effective_period.start is None
    assert [row.source.pointer for row in candidates] == [row['observationRef'] for row in rows]


def test_automatic_confirmation_applies_only_to_extracted_results_with_fixed_provenance():
    scope = ProfileScope(user_id='synthetic-user', subject_id='synthetic-subject')
    recorded_at = datetime(2026, 11, 15, tzinfo=UTC)
    event = SimpleNamespace(
        source_type='health_report',
        scope=scope,
        source_id='synthetic-source',
        source_revision=1,
        content_digest='a' * 64,
        occurred_at=recorded_at,
    )
    trusted = dict(verificationStatus='confirmed', confirmationBasis='analysis_public')
    rows = (
        dict(label='ALT', rawValue='24', unit='U/L', observedAt='2026-11-12', **trusted),
        dict(label='Unmapped assay', rawValue='Positive', **trusted),
        dict(label='Finding', rawValue='Example.', kind='narrative', **trusted),
        dict(label='Diagnosis', rawValue='Example.', kind='narrative', narrativeType='diagnosis', **trusted),
        dict(label='AST', rawValue=None, verificationStatus='unconfirmed'),
        dict(label='Weight', rawValue='81', unit='kg', verificationStatus='unconfirmed'),
    )
    observations = tuple({'observationRef': f'/observations/{i}', **row} for i, row in enumerate(rows))
    material = ProfileSourceMaterial(scope, 'health_report', event.source_id, 1, {}, observations)
    mapper = ProfileSourceMaterialMapper()
    policy = HealthFactPolicy()

    candidates, _ = mapper.map(event, material, analysis_recorded_at=recorded_at)
    decisions = [policy.evaluate(row, subject_kind=SubjectKind.SELF) for row in candidates]
    assert [row.fact_status for row in decisions] == ['active'] * 4 + ['pending'] * 2
    assert [row.verification_status for row in decisions] == ['confirmed'] * 4 + ['unconfirmed'] * 2
    assert [row.source.recorded_at for row in candidates] == [None, recorded_at, recorded_at, recorded_at, None, None]
    assert candidates[2].fact_type == 'condition.finding'
    assert candidates[3].fact_type == 'condition.diagnosis'
    assert candidates[4].value is None

    legacy_candidates, _ = mapper.map(event, material)
    assert all(row.source.confirmation_basis is None for row in legacy_candidates)
    assert all(
        policy.evaluate(row, subject_kind=SubjectKind.SELF).fact_status == 'pending' for row in legacy_candidates
    )
