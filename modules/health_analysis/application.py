"""One cancellable HTTP execution and confirmed trace delivery."""

from __future__ import annotations

import asyncio
import uuid
from copy import deepcopy
from datetime import datetime
from datetime import timezone

from modules.health_analysis.basic_info import ConfirmedBasicInfo
from modules.health_analysis.basic_info_sync import CONFIRMATION_OPERATION_PREFIX
from modules.health_analysis.basic_info_sync import BasicInfoSynchronizer
from modules.health_analysis.contracts import AnalysisBusyError
from modules.health_analysis.contracts import AnalysisCapacityError
from modules.health_analysis.contracts import AnalysisGenerationError
from modules.health_analysis.contracts import AnalysisLimits
from modules.health_analysis.contracts import AnalysisSourceScope
from modules.health_analysis.contracts import AnalysisTimeoutError
from modules.health_analysis.contracts import InvalidAnalysisInputError
from modules.health_analysis.contracts import SourceUnavailableError
from modules.health_analysis.contracts import TraceDeliveryError
from modules.health_analysis.contracts import TraceReceipt
from modules.health_analysis.policy import MAX_QUESTIONS
from modules.health_analysis.surveys import InvalidSurveyError
from modules.health_analysis.surveys import decode_surveys
from modules.health_analysis.trace import encode_record
from modules.health_analysis.trace import trace_hash

RESULT_FIELDS = frozenset({'healthReview', 'suggestedMaterials', 'advancedQuestions', 'advancedSurveySchema'})


class HealthAnalysisService:
    def __init__(
        self,
        profile_reader,
        chat_reader,
        generator,
        trace_writer,
        *,
        basic_info_sync=None,
        limits=None,
        request_semaphore=None,
    ):
        self.basic_info_sync = (
            basic_info_sync
            if isinstance(basic_info_sync, BasicInfoSynchronizer)
            else BasicInfoSynchronizer(basic_info_sync)
        )
        self.profile_reader, self.chat_reader = profile_reader, chat_reader
        self.generator, self.trace_writer = generator, trace_writer
        self.limits = limits if limits is not None else AnalysisLimits()
        self.request_semaphore = (
            request_semaphore if request_semaphore is not None else asyncio.Semaphore(self.limits.max_active_requests)
        )
        self._admitted = 0

    async def generate(self, payload: dict, *, request_uuid: str, deadline_at=None) -> dict:
        if self._admitted >= self.limits.max_active_requests + self.limits.max_waiting_requests:
            raise AnalysisBusyError('Health analysis execution capacity exceeded')
        self._admitted += 1
        maximum_deadline = asyncio.get_running_loop().time() + self.limits.total_timeout_seconds
        deadline = asyncio.timeout_at(
            min(maximum_deadline, deadline_at) if deadline_at is not None else maximum_deadline
        )
        try:
            async with deadline:
                # Validate before any source or durable confirmation.
                decoded = self._validate(payload)
                self._check_deadline(deadline.when())
                scope = AnalysisSourceScope(payload['userId'], payload['groupId'])
                self._check_deadline(deadline.when())
                async with self.request_semaphore:
                    self._check_deadline(deadline.when())
                    return await self._execute(payload, decoded, scope, request_uuid, deadline.when())
        except TimeoutError as error:
            if deadline.expired():
                raise AnalysisTimeoutError('Health analysis execution deadline exceeded') from error
            raise
        finally:
            self._admitted -= 1

    @staticmethod
    def _check_deadline(deadline_at):
        if asyncio.get_running_loop().time() >= deadline_at:
            raise AnalysisTimeoutError('Health analysis execution deadline exceeded')

    def _validate(self, payload):
        if not isinstance(payload, dict) or set(payload) != {
            'userId',
            'name',
            'age',
            'gender',
            'groupId',
            'surveyList',
        }:
            raise InvalidAnalysisInputError('Expected the six Health Analysis fields')
        ConfirmedBasicInfo.parse(payload)
        for name in ('userId', 'groupId'):
            value = payload[name]
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > (256 if name == 'userId' else 512)
                or value != value.strip()
            ):
                raise InvalidAnalysisInputError('Invalid user or group identifier')
        try:
            size = len(encode_record(payload))
        except (ValueError, TypeError, RecursionError) as error:
            raise InvalidAnalysisInputError('Request must contain standard JSON') from error
        if size > self.limits.max_input_bytes:
            raise InvalidAnalysisInputError('Request exceeds health analysis input capacity')
        surveys = payload['surveyList']
        if not isinstance(surveys, list) or len(surveys) > self.limits.max_surveys:
            raise InvalidAnalysisInputError('Invalid survey list capacity')
        try:
            decoded = decode_surveys(surveys)
        except InvalidSurveyError as error:
            raise InvalidAnalysisInputError(str(error)) from error
        if len(decoded.asked_questions) > self.limits.max_questions:
            raise InvalidAnalysisInputError('Too many historical questions')
        return decoded

    async def _read(self, reader, scope, deadline_at):
        self._check_deadline(deadline_at)
        try:
            if reader is None:
                raise SourceUnavailableError('not_configured')
            async with asyncio.timeout(self.limits.source_timeout_seconds):
                result = await reader.read(scope)
        except AnalysisTimeoutError:
            raise
        except SourceUnavailableError as error:
            return {'materials': [], 'status': 'unavailable', 'coverage': {'reason': error.category}}
        except TimeoutError:
            return {'materials': [], 'status': 'unavailable', 'coverage': {'reason': 'source_timeout'}}
        if not isinstance(result, dict) or not isinstance(result.get('materials'), list):
            raise TypeError('Source reader violated its material contract')
        frozen = deepcopy(result)
        frozen['retrievedAt'] = datetime.now(timezone.utc).isoformat()
        return frozen

    async def _sources(self, scope, deadline_at):
        tasks = [
            asyncio.create_task(self._read(reader, scope, deadline_at))
            for reader in (self.profile_reader, self.chat_reader)
        ]
        try:
            return await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _execute(self, payload, decoded, scope, request_uuid, deadline_at):
        self._check_deadline(deadline_at)
        run_id = str(uuid.uuid4())
        confirmed_at = datetime.now(timezone.utc)
        info = ConfirmedBasicInfo.parse(payload)
        sync = await self.basic_info_sync.confirm(
            user_id=scope.user_id,
            operation_id=CONFIRMATION_OPERATION_PREFIX + run_id,
            info=info,
            confirmed_at=confirmed_at,
            deadline_at=deadline_at,
        )
        receipt = sync['receipt']
        scope = AnalysisSourceScope(
            scope.user_id,
            scope.group_id,
            receipt['subjectId'],
            lifecycle_epoch=receipt['lifecycleEpoch'],
        )
        self._check_deadline(deadline_at)
        profile, chat = await self._sources(scope, deadline_at)
        self._check_deadline(deadline_at)
        sources = {'profile': profile, 'chat': chat}
        if len(encode_record(sources)) > self.limits.max_source_bytes:
            raise AnalysisCapacityError('Source material exceeds analysis capacity')
        materials = deepcopy(profile['materials'] + chat['materials'])
        for row in decoded.personal_answers:
            materials.append(
                {
                    'id': f'answer:{row["surveyIndex"]}:{row["pageIndex"]}:{row["questionIndex"]}',
                    'kind': 'answer',
                    'content': deepcopy(row),
                    'status': row['status'],
                }
            )
        identities = set()
        for material in materials:
            if (
                not isinstance(material, dict)
                or not isinstance(material.get('id'), str)
                or not material['id']
                or material['id'] in identities
                or 'content' not in material
            ):
                raise TypeError('Source reader returned an invalid material identity')
            identities.add(material['id'])
        snapshot = {
            'run_id': run_id,
            'deadline_at': deadline_at,
            # The generator removes this internal-only guard before constructing
            # every model payload (generation.py); it is not prompt context.
            'query_exclusion_values': [
                scope.user_id,
                scope.group_id,
                scope.subject_id,
                *scope.patient_sender_ids,
                info.name,
            ],
            'model_redaction_names': [info.name],
            'personal_materials': materials,
            'asked_questions': deepcopy(decoded.asked_questions),
            'has_personal_data': bool(materials),
            'has_historical_schemas': decoded.has_historical_schemas,
            'profile_gender': info.model_context(confirmed_at)['gender'],
            'effective_demographics': info.model_context(confirmed_at),
            'source_coverage': {key: value.get('coverage', {}) for key, value in sources.items()},
        }
        generated = deepcopy(await self.generator.generate(deepcopy(snapshot)))
        self._check_deadline(deadline_at)
        if not isinstance(generated, dict) or not isinstance(generated.get('result'), dict):
            raise AnalysisGenerationError('Generator returned no valid result')
        result = generated['result']
        if set(result) != RESULT_FIELDS or any(
            not isinstance(result[field], str) for field in RESULT_FIELDS if field != 'advancedSurveySchema'
        ):
            raise AnalysisGenerationError('Generator violated the four-field result contract')
        if result['advancedSurveySchema'] is not None and not isinstance(result['advancedSurveySchema'], dict):
            raise AnalysisGenerationError('Generator returned an invalid survey')
        if result['advancedSurveySchema'] is not None:
            try:
                published = decode_surveys([{'schema': result['advancedSurveySchema'], 'answer': {}}])
            except InvalidSurveyError as error:
                raise AnalysisGenerationError('Generator returned an invalid survey') from error
            if len(published.asked_questions) > MAX_QUESTIONS or any(
                row['question']['type'] not in ('radiogroup', 'checkbox') or row['question'].get('isRequired', False)
                for row in published.asked_questions
            ):
                raise AnalysisGenerationError('Generator violated question publication policy')
        analysis_trace = generated.get('trace', {})
        if not isinstance(analysis_trace, dict):
            raise AnalysisGenerationError('Generator returned an invalid analysis trace')
        effective_personal = analysis_trace.get('effectiveHasPersonalData', bool(materials))
        if not isinstance(effective_personal, bool):
            raise AnalysisGenerationError('Generator returned an invalid material classification')
        snapshot['has_candidate_materials'] = bool(materials)
        snapshot['has_personal_data'] = effective_personal
        if not effective_personal and any(
            result[field] for field in ('healthReview', 'suggestedMaterials', 'advancedQuestions')
        ):
            raise AnalysisGenerationError('Empty personal data cannot produce individual analysis')
        record = {
            'contractVersion': 'health-analysis-trace-v2',
            'runId': run_id,
            'uuid': request_uuid,
            'createdAt': datetime.now(timezone.utc).isoformat(),
            'sourceScope': {
                'userId': scope.user_id,
                'groupId': scope.group_id,
                'subjectId': scope.subject_id,
                'lifecycleEpoch': scope.lifecycle_epoch,
                'patientSenderIds': list(scope.patient_sender_ids),
            },
            'confirmedBasicInfo': {
                **info.values(),
                'confirmedAt': confirmed_at.isoformat(),
                'sourceReliability': 'very_high',
            },
            'basicInfoSync': sync,
            'snapshot': snapshot,
            'sourceSnapshots': sources,
            'analysis': analysis_trace,
            'modelIdentity': generated.get('model_identity', {}),
            'result': deepcopy(result),
        }
        if len(encode_record(record)) > self.limits.max_trace_bytes:
            raise TraceDeliveryError('Analysis trace exceeds delivery capacity')
        digest = trace_hash(record)
        if self.trace_writer is None:
            raise TraceDeliveryError('Analysis trace store is not configured')
        for attempt in range(self.limits.trace_attempts):
            self._check_deadline(deadline_at)
            try:
                async with asyncio.timeout(self.limits.trace_timeout_seconds):
                    receipt = await self.trace_writer.write(deepcopy(record))
            except AnalysisTimeoutError:
                raise
            except (TraceDeliveryError, TimeoutError):
                self._check_deadline(deadline_at)
                if attempt + 1 == self.limits.trace_attempts:
                    raise TraceDeliveryError('Analysis trace could not be confirmed') from None
                continue
            self._check_deadline(deadline_at)
            if (
                not isinstance(receipt, TraceReceipt)
                or receipt.durable is not True
                or receipt.run_id != run_id
                or receipt.content_hash != digest
            ):
                raise TraceDeliveryError('Analysis trace was not durably confirmed')
            self._check_deadline(deadline_at)
            return deepcopy(result)
        raise TraceDeliveryError('Analysis trace could not be confirmed')
