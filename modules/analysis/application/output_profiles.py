"""Versioned output strategies. Business-owned extractors are injected, never imported."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

from modules.analysis.contracts import DocumentInputSnapshot
from modules.analysis.contracts import OutputCacheConflictError
from modules.analysis.contracts import OutputRegistrationConflictError
from modules.analysis.contracts import OutputSelection
from modules.analysis.contracts import UnsupportedOutputVersionError
from utils.report_content import report_public_content
from utils.report_content import report_source_content

from .document_reuse import DocumentReuseCoordinator
from .document_reuse import _CheckpointTrace
from .documents import DocumentBusyError
from .documents import DocumentInterpretationError
from .documents import DocumentParsingError
from .documents import DocumentProcessingTrace


@dataclass(frozen=True, slots=True)
class DocumentSnapshot:
    document_id: str
    report: dict
    text: str
    file_name: str
    file_type: str
    content_sha256: str
    content: bytes | None = None
    input_snapshot: DocumentInputSnapshot | None = None


@dataclass(frozen=True, slots=True)
class OutputProfile:
    name: str
    version: str
    extractor: object
    needs_bytes: bool = False
    contract_version: str = '1'

    @property
    def selection(self):
        return OutputSelection(self.name, self.contract_version, self.version)


class DocumentOutputProfiles:
    def __init__(self):
        self._profiles = {}
        self._legacy = {}

    def register(self, profile: OutputProfile, *, legacy=True):
        if not isinstance(profile, OutputProfile) or not callable(profile.extractor):
            raise ValueError('输出格式必须提供名称、版本和提取器')
        selection = profile.selection
        if profile.name == 'health_report' or selection in self._profiles:
            raise OutputRegistrationConflictError('输出格式版本已注册或为保留名称')
        if legacy and profile.name in self._legacy:
            raise OutputRegistrationConflictError('旧输出别名已固定，新增版本必须使用 legacy=False')
        self._profiles[selection] = profile
        if legacy:
            self._legacy[profile.name] = selection

    def register_report_source(self, extractor, *, version, needs_bytes=True, contract_version='1', legacy=True):
        """Register a business-owned extractor; additional revisions never move the legacy alias."""
        self.register(OutputProfile('report_source', version, extractor, needs_bytes, contract_version), legacy=legacy)

    def get(self, selection):
        if isinstance(selection, str):
            selection = self._legacy.get(selection)
        if not isinstance(selection, OutputSelection) or selection not in self._profiles:
            raise UnsupportedOutputVersionError('尚未注册指定文件输出格式及版本')
        return self._profiles[selection]

    def is_legacy(self, selection):
        return self._legacy.get(selection.profile_name) == selection


def normalize_report_source(value, snapshots):
    """Retain the collaborator's intrinsic schema; metadata always comes from server bytes."""
    if not isinstance(value, dict):
        raise DocumentParsingError('Report 来源提取器必须返回 dict')
    result = {
        key: deepcopy(value[key])
        for key in (
            'subject',
            'sample',
            'metrics',
            'taxa',
            'diseaseResults',
            'panelCounts',
            'enterotype',
            'sourceSummary',
            'supplementaryHospitalData',
        )
        if key in value
    }
    for key in ('subject', 'sample'):
        result.setdefault(key, {})
        if not isinstance(result[key], dict):
            raise DocumentParsingError(f'Report 来源 {key} 必须为对象')
    for key in ('metrics', 'taxa', 'diseaseResults', 'panelCounts', 'supplementaryHospitalData'):
        result.setdefault(key, [])
        if not isinstance(result[key], list) or any(not isinstance(item, dict) for item in result[key]):
            raise DocumentParsingError(f'Report 来源 {key} 必须为对象数组')
    result.setdefault('enterotype', None)
    result.setdefault('sourceSummary', None)
    first = snapshots[0]
    result.update(
        {
            'sourceType': first.file_type,
            'sourceName': first.file_name if len(snapshots) == 1 else ' / '.join(item.file_name for item in snapshots),
            'sourceChecksum': first.content_sha256 if len(snapshots) == 1 else None,
            'sourceDocuments': [
                {'documentId': item.document_id, 'sourceName': item.file_name, 'sourceChecksum': item.content_sha256}
                for item in snapshots
            ],
        }
    )
    return result


def input_processing(reports):
    states = [report.get('processing') for report in reports]
    states = [item for item in states if isinstance(item, dict)]
    incomplete = [item for item in states if item.get('status') in {'partial', 'unavailable'}]
    if not incomplete:
        return None
    status = 'unavailable' if any(item.get('status') == 'unavailable' for item in incomplete) else 'partial'
    return {
        'status': status,
        'retryable': any(item.get('retryable', True) for item in incomplete),
        'issueCount': sum(item.get('issueCount', 0) for item in incomplete),
    }


class DocumentOutputCoordinator(DocumentReuseCoordinator):
    def __init__(self, service, index, profiles, *, task_spawner=None):
        self.service, self.index, self.profiles = service, index, profiles
        self.task_spawner = task_spawner or asyncio.create_task
        self.tasks = set()

    def resolve_profile(self, *, output_profile=None, output_selection=None, default='health_report'):
        if output_selection is not None and not isinstance(output_selection, OutputSelection):
            raise ValueError('output_selection 必须为 OutputSelection')
        name = (
            output_profile
            if output_profile is not None
            else (output_selection.profile_name if output_selection else default)
        )
        if output_selection is not None and output_selection.profile_name != name:
            raise ValueError('output_profile 与 output_selection 不一致')
        if name == 'health_report':
            selection = OutputSelection(name, '1', self.service._interpreter.VERSION)
            if output_selection is not None and output_selection != selection:
                raise UnsupportedOutputVersionError('the temporary default extraction identity is unavailable')
            return OutputProfile(name, selection.implementation_revision, None)
        return self.profiles.get(output_selection if output_selection is not None else name)

    def _check_interpreter(self, revision):
        if revision != self.service._interpreter.VERSION:
            raise UnsupportedOutputVersionError('the current configured interpreter does not match the execution')
        return self.service._interpreter

    @staticmethod
    def _failed_error(document):
        if document.get('errorType') == 'UnsupportedOutputVersionError':
            return UnsupportedOutputVersionError('固定解读实现不可用，不能使用当前版本恢复')
        return DocumentReuseCoordinator._failed_error(document)

    @staticmethod
    def _key(identity):
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode('utf-8')).hexdigest()

    def _legacy_identity(self, outcomes, profile, hint, execution_revision):
        # Historical v1 keys used a deployment constant. Reuse them only when
        # persisted provenance proves which interpreter actually made the input.
        if profile.contract_version != '1':
            return None
        if profile.name != 'health_report' and not self.profiles.is_legacy(profile.selection):
            return None
        snapshots = [item.input_snapshot for item in outcomes]
        if any(item is None or not item.interpretation_revision for item in snapshots):
            return None
        versions = {item.interpretation_revision for item in snapshots}
        if any(item.validation_revision != item.interpretation_revision for item in snapshots):
            return None
        identity = {
            'documents': [item.document_id for item in outcomes],
            'sourceRevisions': [item.source_revision for item in outcomes],
            'outputProfile': profile.name,
            'extractorVersion': profile.version,
            'reportTypeHint': hint,
        }
        if execution_revision is not None:
            identity['interpretationVersion'] = execution_revision
        elif profile.name == 'report_source':
            if len(versions) != 1:
                return None
            identity['interpretationVersion'] = next(iter(versions))
        return identity

    async def publish_prepared_report_v1(
        self,
        outcomes,
        *,
        user_id,
        subject_id,
        output_selection,
        fixed_originals,
        prepared_projection,
        preparation_ref,
        preparation_trace_id,
    ):
        """Publish a Document-verified fixed v16 output without another extraction.

        Only Formal v2 calls this entry after checking the immutable preparation
        and actual legacy base content. It does not authorize arbitrary extractor
        aliases or rewrite existing output. The original coordinator identity,
        lease and legacy sink remain authoritative.
        """
        from modules.analysis.report_publication_contracts import LEGACY_REPORT_SOURCE_SELECTION

        if output_selection != LEGACY_REPORT_SOURCE_SELECTION:
            raise UnsupportedOutputVersionError('prepared publication only supports the fixed legacy v16 selection')
        if not isinstance(prepared_projection, dict) or any(
            not isinstance(value, str) or not value or value.strip() != value
            for value in (preparation_ref, preparation_trace_id)
        ):
            raise ValueError('a fixed prepared projection and its actual execution trace are required')
        prepared = {
            'projection': deepcopy(prepared_projection),
            'projectionRef': preparation_ref,
            'outputTraceId': preparation_trace_id,
        }
        return await self._process(
            deepcopy(tuple(outcomes)),
            user_id=user_id,
            subject_id=subject_id,
            output_selection=output_selection,
            fixed_originals=fixed_originals,
            prepared_output=prepared,
        )

    async def _check_prepared_cache(self, record, prepared):
        if prepared is not None and record is not None and 'output_result' in record.get('payloadRefs', {}):
            if await self._payload(record, 'output_result') != prepared['projection']:
                raise OutputCacheConflictError('the existing legacy output differs from the fixed prepared projection')

    async def process(
        self,
        outcomes,
        *,
        user_id,
        subject_id,
        output_profile=None,
        output_selection=None,
        report_type_hint=None,
        fixed_originals=None,
    ):
        return await self._process(
            outcomes,
            user_id=user_id,
            subject_id=subject_id,
            output_profile=output_profile,
            output_selection=output_selection,
            report_type_hint=report_type_hint,
            fixed_originals=fixed_originals,
        )

    async def _process(
        self,
        outcomes,
        *,
        user_id,
        subject_id,
        output_profile=None,
        output_selection=None,
        report_type_hint=None,
        fixed_originals=None,
        prepared_output=None,
    ):
        profile = self.resolve_profile(output_profile=output_profile, output_selection=output_selection)
        if fixed_originals is not None:
            if profile.name != 'report_source' or report_type_hint is not None:
                raise ValueError('fixed originals only support interpretation-free Report extraction')
            if not isinstance(fixed_originals, tuple) or len(fixed_originals) != len(outcomes):
                raise ValueError('fixed originals must follow the exact ordered inputs')
            for outcome, content in zip(outcomes, fixed_originals):
                if not isinstance(content, bytes) or hashlib.sha256(content).hexdigest() != outcome.content_sha256:
                    raise DocumentParsingError('固定原始文件与扩展输入不一致')
        if profile.name == 'health_report' and len(outcomes) != 1:
            raise ValueError('health_report 按单份文件输出')
        if report_type_hint is not None and len(outcomes) != 1:
            raise ValueError('组合提取的类型提示应在逐文件处理时设置')
        execution_revision = (
            self.service._interpreter.VERSION
            if report_type_hint is not None or profile.name == 'health_report'
            else None
        )
        identity = {
            'cacheIdentityVersion': 2,
            'userId': user_id,
            'subjectId': subject_id,
            'documents': [item.document_id for item in outcomes],
            'sourceRevisions': [item.source_revision for item in outcomes],
            'outputProfile': profile.name,
            'contractVersion': profile.contract_version,
            'extractorVersion': profile.version,
            'reportTypeHint': report_type_hint,
            'interpretationExecutionVersion': execution_revision,
            'inputSnapshots': [
                {
                    'contentSha256': item.content_sha256,
                    'snapshot': asdict(item.input_snapshot) if item.input_snapshot is not None else None,
                    'reportDigest': self._key(
                        {
                            key: value
                            for key, value in item.report.items()
                            if key not in {'reportId', 'userId', 'subjectId', 'documentId'}
                        }
                    ),
                }
                for item in outcomes
            ],
        }
        key = self._key(identity)
        legacy_identity = self._legacy_identity(outcomes, profile, report_type_hint, execution_revision)
        legacy_key = self._key(legacy_identity) if legacy_identity is not None else None
        record = await self.index.get(f'extraction_{key}', user_id, subject_id)
        if record is not None and any(record.get(field) != value for field, value in identity.items()):
            raise DocumentParsingError('文件输出缓存身份与固定输入不一致')
        if (
            record is None
            and profile.name == 'report_source'
            and profile.contract_version == '1'
            and self.profiles.is_legacy(profile.selection)
            and report_type_hint is None
        ):
            # With no hint, the deployment interpreter version never executed in
            # this extension. Old records may have mislabeled historical or mixed
            # inputs. The immutable source revisions identify those exact inputs.
            legacy_input = {
                field: identity[field]
                for field in ('documents', 'sourceRevisions', 'outputProfile', 'extractorVersion', 'reportTypeHint')
            }
            candidates = await self.index.find_legacy_outputs(
                user_id=user_id, subject_id=subject_id, identity=legacy_input
            )
            if len(candidates) > 1:
                raise OutputCacheConflictError('同一固定输入存在多个旧输出，请显式核对后迁移')
            record = candidates[0] if candidates else None
        elif record is None and legacy_key is not None:
            candidate = await self.index.get(f'extraction_{legacy_key}', user_id, subject_id)
            if candidate is not None and all(candidate.get(field) == value for field, value in legacy_identity.items()):
                record = candidate
        await self._check_prepared_cache(record, prepared_output)
        trace = DocumentProcessingTrace(self.service._document_trace_store, user_id=user_id)
        await trace.start(
            subjectId=subject_id,
            outputProfile=profile.name,
            contractVersion=profile.contract_version,
            extractorVersion=profile.version,
            extractionKey=record['extractionKey'] if record is not None else key,
            documents=identity['documents'],
            inputTraceIds=[item.trace_id for item in outcomes],
            **(
                {
                    'preparedProjectionRef': prepared_output['projectionRef'],
                    'preparedOutputTraceId': prepared_output['outputTraceId'],
                    'outputReuseRevision': 'document-formal-prepared-v16-v1',
                    'preparedInputExcludedArchiveFields': [
                        'reportId',
                        'userId',
                        'subjectId',
                        'documentId',
                        'sourceRevision',
                    ],
                }
                if prepared_output is not None
                else {}
            ),
        )
        if record is None:
            record = await self.index.resolve(
                {
                    '_id': f'extraction_{key}',
                    'extractionKey': key,
                    **identity,
                    # Preserve v1 publication identity while migrating only the
                    # cache. New contract views retain their own existing policy.
                    'publicationBaseKey': legacy_key or key,
                    'byteSize': sum(len(item.text or '') for item in outcomes),
                    'status': 'pending',
                    'payloadRefs': {},
                    'leaseExpiresAt': None,
                    'createdAt': self.now(),
                    'updatedAt': self.now(),
                }
            )
        deadline = asyncio.get_running_loop().time() + self.WAIT_SECONDS
        executed = False
        target_round = self._target_round(record)
        while record['status'] != 'completed' or record.get('interpretationRound', 0) < target_round:
            if record['status'] == 'failed' and record.get('retryable') is False:
                raise self._failed_error(record)
            token, now = uuid4().hex, self.now()
            owned = await self.index.claim(record, token, now, now + timedelta(seconds=self.LEASE_SECONDS))
            if owned:
                try:
                    await self._check_prepared_cache(owned, prepared_output)
                except Exception:
                    await self.index.update_owned(
                        owned, token, self.now(), {'leaseToken': None, 'leaseExpiresAt': None}
                    )
                    raise
                executed = True
                coroutine = self._execute_output(
                    owned,
                    token,
                    trace,
                    outcomes,
                    profile,
                    report_type_hint,
                    fixed_originals=fixed_originals,
                    prepared_output=prepared_output,
                )
                try:
                    task = self.task_spawner(coroutine)
                except Exception:
                    coroutine.close()
                    await self.index.update_owned(
                        owned, token, self.now(), {'status': 'failed', 'leaseToken': None, 'leaseExpiresAt': None}
                    )
                    raise
                self.tasks.add(task)
                task.add_done_callback(self._task_done)
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task), max(0.01, deadline - asyncio.get_running_loop().time())
                    )
                except TimeoutError as error:
                    raise DocumentBusyError('文件扩展结果仍在处理中') from error
            else:
                if asyncio.get_running_loop().time() >= deadline:
                    raise DocumentBusyError('文件扩展结果仍在处理中')
                await asyncio.sleep(self.POLL_SECONDS)
            record = await self.index.get(record['_id'], user_id, subject_id)
            if record['status'] == 'failed':
                raise self._failed_error(record)
        await self._check_prepared_cache(record, prepared_output)
        output = await self._payload(record, 'output_result')
        processing = input_processing([item.report for item in outcomes] + [output])
        if profile.name == 'report_source' and processing is not None:
            inherited = input_processing([item.report for item in outcomes]) or {}
            # output.processing already summarizes its inputs; do not count the
            # same pending issues twice when reading a cached extension.
            processing['issueCount'] = max(
                inherited.get('issueCount', 0), (output.get('processing') or {}).get('issueCount', 0)
            )
            output = {**output, 'processing': processing}
            await trace.update(
                status='completed',
                stage='output_ready',
                extractionId=record['_id'],
                sourceRevision=None,
                profileIngestion={'status': 'not_submitted', 'jobId': None},
            )
            return replace(
                outcomes[0],
                structured_data=report_public_content(output),
                output_profile=profile.name,
                output_selection=profile.selection,
                extraction_id=record['_id'],
                source_revision=None,
                ingestion_status='not_submitted',
                ingestion_job_id=None,
                output_trace_id=trace.trace_id,
                output_reused=prepared_output is not None or not executed,
            )
        quality, retryable = self._quality(output)
        if quality == 'unavailable':
            await trace.update(stage='interpreting', status='failed', errorType='DocumentInterpretationError')
            raise DocumentInterpretationError(
                '指定类型的文件暂无可用解读结果',
                processing=output.get('processing'),
                retryable=retryable,
                document_id=outcomes[0].document_id,
                trace_id=trace.trace_id,
            )
        source_result = await self._payload(record, 'source_result')
        publication_key = record.get('publicationKey', record.get('publicationBaseKey', record['extractionKey']))
        if 'verifiedObservationPointers' in source_result:
            source_result = {k: v for k, v in source_result.items() if k != 'verifiedObservationPointers'}
            publication_key = f'{publication_key}:all-structured-observations'
        saved = await self.service._legacy_publication.save_revision(
            canonical_report_id=outcomes[0].canonical_report_id,
            user_id=user_id,
            subject_id=subject_id,
            source_key=publication_key,
            result=source_result,
        )
        await trace.update(
            status='completed',
            stage='output_ready',
            extractionId=record['_id'],
            sourceRevision=saved.record.source_revision,
            profileIngestion={'status': saved.record.ingestion_status, 'jobId': saved.record.ingestion_job_id},
        )
        return replace(
            outcomes[0],
            structured_data=report_public_content(output),
            output_profile=profile.name,
            output_selection=profile.selection,
            extraction_id=record['_id'],
            source_revision=saved.record.source_revision,
            ingestion_status=saved.record.ingestion_status,
            ingestion_job_id=saved.record.ingestion_job_id,
            output_trace_id=trace.trace_id,
            output_reused=prepared_output is not None or not executed,
        )

    async def _execute_output(
        self, document, token, trace, outcomes, profile, hint, *, fixed_originals=None, prepared_output=None
    ):
        checkpoint = _CheckpointTrace(self, document, token, trace)
        heartbeat = asyncio.create_task(self._heartbeat(checkpoint))
        try:
            generated = 'output_result' not in document['payloadRefs'] or document.get(
                'outputResultRound', document.get('interpretationRound', 0)
            ) != document.get('interpretationRound', 0)
            if generated and prepared_output is not None:
                await checkpoint.write_payload('output_result', deepcopy(prepared_output['projection']))
                await checkpoint.save(
                    outputResultRound=document.get('interpretationRound', 0),
                    preparedOutput={key: value for key, value in prepared_output.items() if key != 'projection'},
                )
                # No output_raw is fabricated: the actual extractor ran in the
                # linked preparation trace, not in this publication execution.
            if generated and prepared_output is None:
                snapshots = []
                for index, outcome in enumerate(outcomes):
                    content = None
                    if profile.needs_bytes:
                        content = (
                            fixed_originals[index]
                            if fixed_originals is not None
                            else (
                                await self.service.read_document_payload(
                                    outcome.document_id,
                                    user_id=document['userId'],
                                    subject_id=document['subjectId'],
                                )
                            )['content']
                        )
                        if hashlib.sha256(content).hexdigest() != outcome.content_sha256:
                            raise DocumentParsingError('扩展提取的原始文件校验失败')
                    report = deepcopy(outcome.report)
                    for field in ('reportId', 'userId', 'subjectId', 'documentId'):
                        report.pop(field, None)
                    if hint is not None or profile.name == 'health_report':
                        revision = document.get('interpretationExecutionVersion') or document.get(
                            'interpretationVersion'
                        )
                        interpreter = self._check_interpreter(revision)
                        if content is None:
                            content = (
                                await self.service.read_document_payload(
                                    outcome.document_id,
                                    user_id=document['userId'],
                                    subject_id=document['subjectId'],
                                )
                            )['content']
                        if hashlib.sha256(content).hexdigest() != outcome.content_sha256:
                            raise DocumentParsingError('扩展提取的原始文件校验失败')
                        checkpoint.parsed_document = await self.service.read_document_payload(
                            outcome.document_id,
                            user_id=document['userId'],
                            subject_id=document['subjectId'],
                            name='docmind_result',
                        )
                        report = await interpreter.analyze(
                            outcome.text,
                            user_id=document['userId'],
                            subject_id=document['subjectId'],
                            report_type_hint=hint,
                            trace=checkpoint,
                            source_snapshot={
                                'content': content,
                                'fileName': outcome.file_name,
                                'contentSha256': outcome.content_sha256,
                            },
                        )
                        if report is None:
                            raise DocumentInterpretationError('指定类型的文件解读失败')
                    snapshots.append(
                        DocumentSnapshot(
                            outcome.document_id,
                            report,
                            outcome.text,
                            outcome.file_name,
                            outcome.resolved_file_type,
                            outcome.content_sha256,
                            content,
                            outcome.input_snapshot,
                        )
                    )
                if profile.name == 'health_report':
                    output = snapshots[0].report
                else:
                    if inspect.iscoroutinefunction(profile.extractor):
                        output = profile.extractor(tuple(snapshots))
                    else:
                        output = await asyncio.to_thread(profile.extractor, tuple(snapshots))
                    if inspect.isawaitable(output):
                        output = await output
                    if not isinstance(output, dict):
                        raise DocumentParsingError('文件输出格式必须返回 dict')
                    await checkpoint.write_payload('output_raw', output)
                    if profile.name == 'report_source':
                        output = normalize_report_source(output, snapshots)
                        if processing := input_processing([item.report for item in snapshots]):
                            output['processing'] = processing
                await checkpoint.write_payload('output_result', output)
                await checkpoint.save(outputResultRound=document.get('interpretationRound', 0))
            output = await self._payload(document, 'output_result')
            quality, retryable = self._quality(output)
            if profile.name == 'report_source' and (
                quality in {'partial', 'unavailable'}
                or input_processing([item.report for item in outcomes]) is not None
            ):
                # An independent raw-text extractor cannot reintroduce candidates
                # that the shared evidence validator has quarantined. Preserve its
                # archive for Report diagnostics, with no source capture or queue.
                await checkpoint.save(
                    status='completed',
                    processingStatus=quality,
                    processingRetryable=False,
                    sourceRevision=None,
                    leaseToken=None,
                    leaseExpiresAt=None,
                )
                return
            if quality == 'unavailable':
                await checkpoint.save(
                    status='completed',
                    processingStatus=quality,
                    processingRetryable=retryable,
                    leaseToken=None,
                    leaseExpiresAt=None,
                )
                return
            if generated or 'source_result' not in document['payloadRefs']:
                source_result = (
                    deepcopy(output)
                    if profile.name == 'health_report'
                    else {
                        'reportType': outcomes[0].report.get('reportType', 'unknown'),
                        'reportTime': outcomes[0].report.get('reportTime') if len(outcomes) == 1 else None,
                        'reportSource': output if profile.name == 'report_source' else {},
                    }
                )
                source_result['sourceDocumentIds'] = [item.document_id for item in outcomes]
                await checkpoint.write_payload('source_result', source_result)
            source_result = await self._payload(document, 'source_result')
            # Each recovered output publishes its own immutable revision. The
            # extraction identity alone would pin every recovery to its first result.
            publication_key = document.get('publicationBaseKey', document['extractionKey'])
            if 'processing' in output:
                digest = hashlib.sha256(
                    json.dumps(report_source_content(source_result), ensure_ascii=False, sort_keys=True).encode('utf-8')
                ).hexdigest()
                publication_key = f'{publication_key}:{digest}'
            saved = await self.service._legacy_publication.save_revision(
                canonical_report_id=outcomes[0].canonical_report_id,
                user_id=document['userId'],
                subject_id=document['subjectId'],
                source_key=publication_key,
                result=source_result,
            )
            await checkpoint.save(publicationKey=publication_key, sourceRevision=saved.record.source_revision)
            await checkpoint.update(stage='completed', status='completed')
            await checkpoint.save(
                status='completed',
                processingStatus=quality,
                processingRetryable=retryable,
                leaseToken=None,
                leaseExpiresAt=None,
            )
        except (Exception, asyncio.CancelledError) as error:
            with suppress(Exception):
                await checkpoint.save(
                    status='failed',
                    errorType=type(error).__name__,
                    leaseToken=None,
                    leaseExpiresAt=None,
                    retryable=getattr(error, 'retryable', True),
                    automaticRetryable=getattr(error, 'automatic_retryable', True),
                    contentFailure=(
                        getattr(error, 'content_failure', None) if isinstance(error, DocumentParsingError) else False
                    ),
                )
            with suppress(Exception):
                await trace.update(status='failed', errorType=type(error).__name__)
            raise
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
