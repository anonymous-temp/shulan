"""Report consumes immutable Document inputs; projections belong to Report."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.analysis.application.output_profiles import input_processing
from modules.analysis.application.output_profiles import normalize_report_source
from modules.analysis.contracts import OutputSelection
from modules.analysis.contracts import UnsupportedOutputVersionError
from modules.report.document_projection import REPORT_SOURCE_SELECTION
from modules.report.document_projection import ReportProjectionService
from modules.report.document_projection import ReportSourceInput
from modules.report.document_projection import normalize_report_projection
from modules.report.document_source import extract_intrinsic_report_source
from tests.unit.services.analysis.test_document_understanding import environment
from tests.unit.services.analysis.test_document_understanding import prepare
from tests.unit.services.analysis.test_document_understanding import stored
from utils.report_content import report_public_content


class Cache:
    def __init__(self):
        self.rows = {}
        self.get_count = 0

    async def get(self, key):
        self.get_count += 1
        return deepcopy(self.rows.get(key))

    async def put_once(self, key, value):
        if key in self.rows and self.rows[key] != value:
            raise ValueError('conflicting immutable output')
        self.rows[key] = deepcopy(value)
        return deepcopy(value)


async def frozen_input():
    env = environment()
    bundle = await prepare(env)
    metadata = bundle.material.payload['documentMetadata']
    original = env.flow.traces.payloads[(metadata['originalArtifactRef'], 'original_file')]['content']
    return env, bundle, ReportSourceInput.from_bundle(bundle, original)


@pytest.mark.asyncio
async def test_real_v16_projection_matches_legacy_and_does_not_publish():
    env, bundle, source = await frozen_input()
    actual = await ReportProjectionService(Cache()).project([source], selection=REPORT_SOURCE_SELECTION)
    expected = normalize_report_source(extract_intrinsic_report_source((source,)), (source,))
    assert actual == expected
    assert not env.flow.collection.documents and not env.flow.jobs.jobs
    assert bundle.facts.snapshot_id in source._manifest_json
    assert len(env.flow.client.submit_calls) == len(env.flow.graph.calls) == 1


@pytest.mark.asyncio
async def test_cache_hit_miss_restart_pin_real_implementation_and_cannot_follow_latest():
    env, bundle, source = await frozen_input()
    cache = Cache()
    old = AsyncMock(return_value={'subject': {'name': 'original projection'}})
    new = AsyncMock(return_value={'subject': {'name': 'new report projection'}})
    v2 = OutputSelection('report_source', '2', 'report-source-next')
    service = ReportProjectionService(cache, implementations={REPORT_SOURCE_SELECTION: old, v2: new})
    first = await service.project([source], selection=REPORT_SOURCE_SELECTION)
    second = await service.project([source], selection=v2)
    assert first['subject'] != second['subject']
    # A restart has both real implementations; cache bindings cannot alias.
    restored = ReportProjectionService(cache, implementations={REPORT_SOURCE_SELECTION: old, v2: new})
    assert await restored.project([source], selection=REPORT_SOURCE_SELECTION) == first
    assert await restored.project([source], selection=v2) == second
    old.assert_awaited_once()
    new.assert_awaited_once()
    assert len(cache.rows) == 2
    source.report.clear()
    assert source.report == report_public_content(bundle.understanding.payload['interpretation'])
    assert not env.flow.collection.documents


@pytest.mark.asyncio
async def test_unavailable_version_fails_even_when_an_old_cache_exists():
    _, _, source = await frozen_input()
    cache = Cache()
    await ReportProjectionService(cache).project([source], selection=REPORT_SOURCE_SELECTION)
    before = cache.get_count
    with pytest.raises(UnsupportedOutputVersionError):
        await ReportProjectionService(cache, implementations={}).project([source], selection=REPORT_SOURCE_SELECTION)
    assert cache.get_count == before


@pytest.mark.asyncio
async def test_corrupt_cache_or_original_never_uses_a_new_document():
    _, bundle, source = await frozen_input()
    with pytest.raises(ValueError, match='Original bytes'):
        ReportSourceInput.from_bundle(bundle, b'wrong original')
    cache = Cache()
    service = ReportProjectionService(cache)
    await service.project([source], selection=REPORT_SOURCE_SELECTION)
    next(iter(cache.rows.values()))['output']['subject'] = {'name': 'corrupt'}
    with pytest.raises(ValueError, match='cache'):
        await service.project([source], selection=REPORT_SOURCE_SELECTION)


@pytest.mark.asyncio
async def test_order_and_input_list_are_frozen_before_cache_await():
    _, _, source = await frozen_input()
    second = replace(
        source,
        _manifest_json=source._manifest_json.replace(source.file_name, 'second.pdf').replace(
            source.document_id, source.document_id + '-second'
        ),
    )
    inputs = [source, second]
    cache = Cache()
    get = cache.get

    async def mutate(key):
        inputs.reverse()
        return await get(key)

    cache.get = mutate
    extractor = AsyncMock(return_value={})
    service = ReportProjectionService(cache, implementations={REPORT_SOURCE_SELECTION: extractor})
    output = await service.project(inputs, selection=REPORT_SOURCE_SELECTION)
    assert output['sourceName'] == source.file_name + ' / second.pdf'
    assert extractor.await_args.args[0] == (source, second)
    with pytest.raises(TypeError):
        service.implementations[REPORT_SOURCE_SELECTION] = AsyncMock()


@pytest.mark.asyncio
async def test_mixed_scope_is_rejected_before_cache_io():
    _, _, source = await frozen_input()
    second = replace(source, _manifest_json=source._manifest_json.replace('"userId":"user"', '"userId":"other"'))
    if second.manifest == source.manifest:
        import json

        changed = source.manifest
        changed['scope']['userId'] = 'other-user'
        second = replace(source, _manifest_json=json.dumps(changed))
    cache = Cache()
    with pytest.raises(PermissionError):
        await ReportProjectionService(cache).project([source, second], selection=REPORT_SOURCE_SELECTION)
    assert not cache.get_count


@pytest.mark.parametrize(
    'states',
    [
        [{}, {'status': 'complete'}],
        [{'status': 'partial', 'issueCount': 2, 'retryable': False}, {}],
        [{'status': 'unavailable', 'issueCount': 3}, {'status': 'partial', 'issueCount': 2}],
    ],
)
def test_owned_normalizer_preserves_quality_and_metadata_order(states):
    snapshots = tuple(
        SimpleNamespace(
            report={'processing': state},
            file_name=f'{i}.png',
            file_type='png',
            document_id=str(i),
            content_sha256=f'hash-{i}',
        )
        for i, state in enumerate(states)
    )
    raw = {'metrics': [{'name': 'synthetic'}], 'unknown_private_extension': True}
    expected = normalize_report_source(raw, snapshots)
    if processing := input_processing([item.report for item in snapshots]):
        expected['processing'] = processing
    assert normalize_report_projection(raw, snapshots) == expected
    assert 'unknown_private_extension' not in normalize_report_projection(raw, snapshots)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'revision,allowed',
    [
        ('analysis-health-report-v5', True),
        ('analysis-health-report-v6', False),
        ('missing', False),
    ],
)
async def test_raw_fallback_version_is_owned_by_report_and_fixed_in_input(revision, allowed):
    from modules.analysis.snapshots import canonical_json

    _, bundle, source = await frozen_input()
    payload = bundle.material.payload
    payload.update(
        interpretationRevision=revision, validationRevision=revision, failureTrace={'traceId': 'fixed-failed-call'}
    )
    material = replace(bundle.material, _payload_json=canonical_json(payload))
    if not allowed:
        with pytest.raises(UnsupportedOutputVersionError):
            ReportSourceInput.from_failed_material(material, source.content)
        return
    fallback = ReportSourceInput.from_failed_material(material, source.content)
    assert fallback.manifest['interpretationRevision'] == revision
    assert fallback.report == {} and fallback.manifest['mode'] == 'legacy_fallback'
    assert fallback.text == source.text


@pytest.mark.asyncio
async def test_quarantined_input_cannot_be_combined_with_raw_fallback():
    import json

    _, _, source = await frozen_input()
    manifest = source.manifest
    manifest['mode'] = 'legacy_fallback'
    fallback = replace(source, _manifest_json=json.dumps(manifest), _report_json='{}')
    partial = replace(source, _report_json='{"processing":{"status":"partial"}}')
    cache = Cache()
    with pytest.raises(ValueError, match='quarantined'):
        await ReportProjectionService(cache).project([fallback, partial], selection=REPORT_SOURCE_SELECTION)
    assert not cache.get_count


@pytest.mark.asyncio
async def test_mongo_projection_store_handles_acknowledgement_and_conflicting_races():
    from pymongo.errors import DuplicateKeyError

    from adapters.report.projection_store import MongoReportProjectionStore

    collection = SimpleNamespace(
        insert_one=AsyncMock(return_value=SimpleNamespace(acknowledged=True)),
        find_one=AsyncMock(),
    )
    store = MongoReportProjectionStore(collection)
    row = {'binding': {'revision': 1}, 'output': {'sourceName': 'fixed.pdf'}}
    saved = await store.put_once('fixed', row)
    saved['output'].clear()
    assert row['output'] == {'sourceName': 'fixed.pdf'}
    collection.insert_one.side_effect = DuplicateKeyError('race')
    collection.find_one.return_value = deepcopy(row)
    assert await store.put_once('fixed', row) == row
    collection.find_one.return_value = {'output': {'sourceName': 'other.pdf'}}
    with pytest.raises(ValueError, match='conflicting'):
        await store.put_once('fixed', row)
    collection.insert_one.side_effect = None
    collection.insert_one.return_value = SimpleNamespace(acknowledged=False)
    with pytest.raises(RuntimeError, match='acknowledged'):
        await store.put_once('fixed', row)


@pytest.mark.asyncio
async def test_duplicate_documents_keep_accepted_order_but_execute_once_like_legacy():
    _, _, source = await frozen_input()
    extractor = AsyncMock(return_value={})
    cache = Cache()
    result = await ReportProjectionService(cache, implementations={REPORT_SOURCE_SELECTION: extractor}).project(
        [source, source],
        selection=REPORT_SOURCE_SELECTION,
    )
    assert extractor.await_args.args[0] == (source,)
    assert len(result['sourceDocuments']) == 1 and result['sourceName'] == source.file_name
    assert len(next(iter(cache.rows.values()))['binding']['inputs']) == 2


@pytest.mark.asyncio
async def test_one_document_cannot_silently_select_between_conflicting_fixed_snapshots():
    _, _, source = await frozen_input()
    other = replace(source, _report_json='{"processing":{"status":"complete"},"newRevision":true}')
    cache = Cache()
    with pytest.raises(ValueError, match='conflicting fixed'):
        await ReportProjectionService(cache).project([source, other], selection=REPORT_SOURCE_SELECTION)
    assert not cache.get_count


@pytest.mark.asyncio
async def test_raw_fallback_group_preserves_duplicates_and_never_uses_interpreted_facts():
    from modules.analysis.snapshots import canonical_json

    _, bundle, source = await frozen_input()
    payload = bundle.material.payload
    payload.update(interpretationRevision='analysis-health-report-v5', failureTrace={'traceId': 'fixed-call'})
    fallback = ReportSourceInput.from_failed_material(
        replace(bundle.material, _payload_json=canonical_json(payload)), source.content
    )
    normal = replace(
        source,
        _manifest_json=source._manifest_json.replace(source.document_id, source.document_id + '-other'),
        _report_json='{"processing":{"status":"complete"},"observations":[{"itemName":"interpreted"}]}',
    )
    extractor = AsyncMock(return_value={})
    cache = Cache()
    result = await ReportProjectionService(cache, implementations={REPORT_SOURCE_SELECTION: extractor}).project(
        [fallback, fallback, normal], selection=REPORT_SOURCE_SELECTION
    )
    executed = extractor.await_args.args[0]
    assert len(executed) == 3 and all(item.report == {} for item in executed)
    assert [item.document_id for item in executed] == [fallback.document_id, fallback.document_id, normal.document_id]
    assert result['sourceName'] == ' / '.join(item.file_name for item in executed)
    assert len(result['sourceDocuments']) == 3
    assert len(next(iter(cache.rows.values()))['binding']['inputs']) == 3
    assert normal.report['observations'] == [{'itemName': 'interpreted'}]


@pytest.mark.asyncio
async def test_v16_preserves_source_approved_assessment_and_full_immutable_provenance():
    from modules.analysis.understanding import UNDERSTANDING_SELECTION

    env, first, source = await frozen_input()
    document, interpretation = stored(env, first)
    interpretation.update(
        {
            'reportType': 'imaging',
            'observations': [
                {
                    'itemName': '影像结论',
                    'resultValue': '肝脏脂肪浸润',
                    'kind': 'assessment',
                    'evidence': {'quote': '肝脏回声增强，临床结论已确认'},
                }
            ],
        }
    )
    bundle = await env.api.capture_bundle(
        scope=first.material.scope,
        document_id=document['_id'],
        selection=UNDERSTANDING_SELECTION,
    )
    fixed = ReportSourceInput.from_bundle(bundle, source.content)
    private = replace(fixed, _report_json=__import__('json').dumps(interpretation))
    assert not extract_intrinsic_report_source((private,))['supplementaryHospitalData']
    expected = normalize_report_source(extract_intrinsic_report_source((fixed,)), (fixed,))
    actual = await ReportProjectionService(Cache()).project((fixed,), selection=REPORT_SOURCE_SELECTION)
    assert actual == expected
    assert actual['supplementaryHospitalData'][0]['examResult'] == '肝脏脂肪浸润'
    assert 'evidence' in bundle.understanding.payload['interpretation']['observations'][0]
    assert fixed.manifest['understandingSnapshotRef'] == bundle.understanding.snapshot_id
    assert fixed.manifest['factsSnapshotRef'] == bundle.facts.snapshot_id
    assert fixed.manifest['materialSnapshotRef'] == bundle.material.snapshot_id
    assert fixed.manifest['interpretationProjectionRevision'] == 'report-understanding-public.v1'


@pytest.mark.asyncio
async def test_public_shape_revision_cannot_reuse_pre_migration_projection_cache():
    import json

    _, _, source = await frozen_input()
    old_manifest = source.manifest
    old_manifest.pop('interpretationProjectionRevision')
    legacy = replace(source, _manifest_json=json.dumps(old_manifest))
    cache = Cache()
    extractor = AsyncMock(
        side_effect=[{'subject': {'name': 'old private shape'}}, {'subject': {'name': 'public shape'}}]
    )
    service = ReportProjectionService(cache, implementations={REPORT_SOURCE_SELECTION: extractor})
    old = await service.project((legacy,), selection=REPORT_SOURCE_SELECTION)
    new = await service.project((source,), selection=REPORT_SOURCE_SELECTION)
    assert old['subject'] != new['subject']
    assert len(cache.rows) == extractor.await_count == 2
    assert await service.project((source,), selection=REPORT_SOURCE_SELECTION) == new
    assert extractor.await_count == 2


@pytest.mark.parametrize('status', ['complete', 'partial', 'unavailable'])
@pytest.mark.parametrize('outcome', [None, 'no_readable_text', 'no_verified_results', 'interpretation_incomplete'])
def test_report_owned_public_shape_matches_legacy_and_is_independent_of_shared_template(monkeypatch, status, outcome):
    from modules.analysis.application import content_results
    from modules.report.document_rendering import report_understanding_content_v1
    from utils import report_content

    child = {
        'reportType': 'lab',
        'analysisEvidence': ['private'],
        'metadataEvidence': {'private': True},
        'observations': [
            {
                'itemName': 'marker',
                'resultValue': '5',
                'evidence': {'quote': 'accepted'},
                'sourceId': 'raw',
                'rawFlag': 'H',
            }
        ],
        'unresolvedItems': [{'itemName': 'missing result', 'evidence': {'quote': 'private'}, 'sourceSection': 0}],
        'processing': {
            'status': status,
            'retryable': True,
            'issueCount': 1,
            'failureKind': 'technical',
            'contentOutcome': outcome,
        },
    }
    source = {
        **deepcopy(child),
        'sections': [child],
        'sourceBindings': ['private'],
        'verifiedObservationPointers': ['private'],
    }
    frozen = deepcopy(source)
    expected = report_public_content(source)
    monkeypatch.setattr(report_content, 'report_public_content', lambda value: {'changed': True})
    monkeypatch.setattr(content_results, 'CONTENT_MESSAGES', {'no_readable_text': 'changed elsewhere'})
    assert report_understanding_content_v1(source) == expected
    assert source == frozen
