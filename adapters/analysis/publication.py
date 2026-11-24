"""Durable publication sequence and legacy gate using single-source-document CAS.

A reserved event stays in its source document until its immutable outbox row is
written. Rescue retractions archive their predecessor before replacing it, so a
crash between those writes is replayable. Historical intents live in a
separate collection rather than an unbounded array in the source document.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from datetime import timezone
from uuid import uuid4

from pymongo.errors import DuplicateKeyError

from modules.analysis.publication import PublicationConflictError
from modules.analysis.publication import PublicationModeError
from modules.analysis.publication import PublicationReceipt
from modules.analysis.publication_recovery import RECOVERY_MATERIAL_CONTRACTS
from modules.analysis.publication_recovery import PublicationLeaseLost
from modules.analysis.publication_recovery import PublicationRecoveryError
from modules.analysis.publication_recovery import PublicationRecoveryPolicy
from modules.analysis.snapshots import COMPLETE_SOURCE_CONTRACT_V2
from modules.analysis.snapshots import RETRACTION_CONTRACT
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import digest

MAX_ACCEPTED_INTERPRETATION_DOCUMENTS = 10_000
CURRENT_NOOP_RECORD_STATE = 'current_noop_acknowledged'
CURRENT_NOOP_PROOF_FIELDS = {
    'commandId',
    'priorPublicationRevision',
    'priorSourceSnapshotRef',
    'priorEventId',
    'priorContentDigest',
    'lifecycleEpoch',
    'interpretationRounds',
    'commandDigest',
}


class MongoDocumentPublicationLedger:
    def __init__(self, *, sources, intents):
        self.sources = sources
        self.intents = intents

    @staticmethod
    def _key(scope, source_id):
        return 'document_source_' + digest([scope.as_dict(), source_id])

    async def read_source(self, scope, source_id):
        return await self.sources.find_one({'_id': self._key(scope, source_id), **scope.as_dict()})

    async def read_sources(self, scope, *, after_id, limit):
        query = scope.as_dict()
        if after_id is not None:
            query['_id'] = {'$gt': after_id}
        return await self.sources.find(query).sort([('_id', 1)]).to_list(length=limit)

    async def _source(self, scope, source_id):
        key = self._key(scope, source_id)
        document = {
            '_id': key,
            **scope.as_dict(),
            'sourceId': source_id,
            'mode': 'legacy',
            'stateVersion': 0,
            'publicationRevision': 0,
            'appliedRevision': 0,
            'acceptedRevision': 0,
            'settledRevision': 0,
            'reservedRetractionRevision': 0,
            'acceptedRetractionRevision': 0,
            'appliedRetractionRevision': 0,
            'outboxPending': False,
            'legacyTickets': {},
            'currentIntent': None,
        }
        try:
            inserted = await self.sources.insert_one(document)
            if not inserted.acknowledged:
                raise RuntimeError('source ledger admission was not acknowledged')
        except DuplicateKeyError:
            pass
        return await self.read_source(scope, source_id)

    async def _cas(self, source, fields):
        result = await self.sources.update_one(
            {'_id': source['_id'], 'stateVersion': source['stateVersion']},
            {'$set': {**fields, 'stateVersion': source['stateVersion'] + 1}},
        )
        if not result.acknowledged:
            raise RuntimeError('publication ledger update was not acknowledged')
        return bool(result.matched_count)

    @staticmethod
    def _interpretation_rounds(value):
        if (
            not isinstance(value, dict)
            or len(value) > MAX_ACCEPTED_INTERPRETATION_DOCUMENTS
            or any(
                not isinstance(document, str)
                or not 0 < len(document) <= 256
                or document.strip() != document
                or '\x00' in document
                or type(round_number) is not int
                or not 0 < round_number < 2**63
                for document, round_number in value.items()
            )
        ):
            raise PublicationConflictError('fixed document interpretation rounds are invalid')
        return dict(value)

    @classmethod
    def _advance_interpretation_rounds(cls, source, submitted):
        accepted = cls._interpretation_rounds(source.get('acceptedInterpretationRounds', {}))
        submitted = cls._interpretation_rounds(submitted)
        if any(round_number < accepted.get(document, 0) for document, round_number in submitted.items()):
            raise PublicationConflictError('publication uses an earlier accepted interpretation round')
        return cls._interpretation_rounds({**accepted, **submitted})

    async def admit_current_source(self, scope, source_id, *, binding, interpretation_rounds=None):
        """Fix the initial current operation; later managed publications keep normal CAS.

        The old producer-ticket check only protects the temporary default composition
        during serial integration. It is not a migration or historical-data scan.
        """
        if binding.get('scope') != scope.as_dict() or binding.get('sourceId') != source_id:
            raise PublicationConflictError('current source binding has a different scope or source')
        binding = deepcopy(binding)
        rounds = self._interpretation_rounds({} if interpretation_rounds is None else interpretation_rounds)
        initial_rounds_digest = digest(rounds)
        for _ in range(32):
            source = await self._source(scope, source_id)
            original = source.get('currentSourceBinding')
            if original is not None:
                if original != binding:
                    raise PublicationConflictError('the original current source admission is fixed')
                if source.get('initialInterpretationRoundsDigest', digest({})) != initial_rounds_digest:
                    raise PublicationConflictError('initial current source interpretation rounds are fixed')
                return original
            if (
                source['mode'] != 'legacy'
                or source['publicationRevision']
                or source['currentIntent']
                or source['legacyTickets']
                or source.get('legacySourceRevision') is not None
            ):
                raise PublicationConflictError('the source already has another active publication owner')
            fields = {
                'mode': 'events',
                'currentSourceBinding': binding,
                'acceptedInterpretationRounds': rounds,
                'initialInterpretationRoundsDigest': initial_rounds_digest,
            }
            if await self._cas(source, fields):
                return binding
        raise PublicationConflictError('current source admission changed concurrently')

    async def begin_legacy(self, scope, source_id, ticket):
        for _ in range(32):
            source = await self._source(scope, source_id)
            if source['mode'] != 'legacy':
                raise PublicationModeError('legacy publication is disabled for this source')
            tickets = {**source['legacyTickets'], ticket: datetime.now(timezone.utc).isoformat()}
            if await self._cas(source, {'legacyTickets': tickets}):
                return
        raise PublicationConflictError('legacy publication admission is busy')

    async def finish_legacy(self, scope, source_id, ticket):
        for _ in range(32):
            source = await self.read_source(scope, source_id)
            if source is None or ticket not in source['legacyTickets']:
                return
            tickets = {k: v for k, v in source['legacyTickets'].items() if k != ticket}
            if await self._cas(source, {'legacyTickets': tickets}):
                return
        raise PublicationConflictError('legacy publication completion is busy')

    async def switch_mode(self, scope, source_id, *, target, evidence):
        if target not in {'legacy', 'events'}:
            raise ValueError('unknown publication mode')
        for _ in range(32):
            source = await self._source(scope, source_id)
            if source.get('freshAdmission') is not None:
                raise PublicationModeError('a fresh admission fence requires its explicit ready activation')
            evidence.validate(source['publicationRevision'])
            if source['legacyTickets']:
                raise PublicationModeError('legacy writes are still in flight or uncertain')
            if source['appliedRevision'] != source['publicationRevision']:
                raise PublicationModeError('a publication is not yet applied')
            if target == 'legacy' and source['publicationRevision']:
                # The old additive sink cannot undo new source-wide corrections
                # or retractions. Roll back binaries with this gate retained.
                raise PublicationModeError('legacy rollback after event publication is unsupported')
            if await self._cas(source, {'mode': target, 'cutoverEvidence': asdict(evidence)}):
                return
        raise PublicationConflictError('publication mode changed concurrently')

    async def hold_fresh_admission(self, scope, source_id, *, request_id, prepared_manifest_ref):
        """Atomically exclude every legacy authorization during fresh verification.

        Work still computing without a ticket cannot subsequently obtain one.
        This does not claim that all parsing/model processes have been stopped.
        """
        if not all(
            isinstance(value, str) and value and value == value.strip() for value in (request_id, prepared_manifest_ref)
        ):
            raise ValueError('fixed fresh admission and prepared manifest identities are required')
        identity = digest([scope.as_dict(), source_id, request_id, prepared_manifest_ref])
        for _ in range(32):
            source = await self._source(scope, source_id)
            held = source.get('freshAdmission')
            if held is not None:
                if source.get('freshAdmissionRequest') != identity:
                    raise PublicationConflictError('source is bound to another fresh admission')
                return held
            if source['mode'] != 'legacy' or source['publicationRevision'] or source['currentIntent']:
                raise PublicationModeError('fresh admission requires no previous event source')
            if source['legacyTickets']:
                raise PublicationModeError('legacy writes are in flight or uncertain')
            held = {
                'fenceId': 'document_fresh_fence_' + digest([identity, source['stateVersion'] + 1]),
                'fenceGeneration': source['stateVersion'] + 1,
                'preparedManifestRef': prepared_manifest_ref,
                'issuedAt': datetime.now(timezone.utc).isoformat(),
            }
            if await self._cas(
                source,
                {
                    'mode': 'admission',
                    'freshAdmission': held,
                    'freshAdmissionRequest': identity,
                    'freshOwnerProofIssued': False,
                },
            ):
                return held
        raise PublicationConflictError('fresh source admission is busy')

    async def issue_fresh_owner_proof(self, scope, source_id, fence):
        """Durable point after which a concurrent Profile authorization is possible."""
        for _ in range(32):
            source = await self.read_source(scope, source_id)
            if source is None or source['mode'] != 'admission' or source.get('freshAdmission') != fence:
                raise PublicationConflictError('the fresh source fence was lost before proof issuance')
            if source.get('freshOwnerProofIssued') is True:
                return
            if await self._cas(source, {'freshOwnerProofIssued': True}):
                return
        raise PublicationConflictError('fresh proof issuance is busy')

    async def abort_unissued_fresh_admission(self, scope, source_id, fence, *, reason):
        """Safely restore legacy only before any owner proof can have escaped.

        A current Profile marker lookup alone is insufficient: an authorization
        could still be inserting that marker after the lookup returned empty.
        """
        for _ in range(32):
            source = await self.read_source(scope, source_id)
            if source is None or source.get('freshAdmission') != fence:
                return False
            if source['mode'] != 'admission' or source.get('freshOwnerProofIssued') is not False:
                return False
            if source['publicationRevision'] or source['currentIntent'] or source['legacyTickets']:
                return False
            if await self._cas(
                source,
                {
                    'mode': 'legacy',
                    'freshAdmission': None,
                    'freshAdmissionRequest': None,
                    'freshAborted': {'fence': fence, 'reason': reason, 'at': datetime.now(timezone.utc).isoformat()},
                },
            ):
                return True
        raise PublicationConflictError('fresh admission rollback is busy')

    async def fresh_admission_held(self, scope, source_id, fence):
        source = await self.read_source(scope, source_id)
        return bool(
            source is not None
            and source['mode'] == 'admission'
            and source.get('freshAdmission') == fence
            and not source['legacyTickets']
            and source['publicationRevision'] == 0
            and source['currentIntent'] is None
        )

    async def activate_fresh_admission(self, scope, source_id, *, fence, ready_proof):
        """Called only after the owner service validates Profile's public ready proof."""
        for _ in range(32):
            source = await self.read_source(scope, source_id)
            if source is None or source.get('freshAdmission') != fence:
                raise PublicationConflictError('the fixed fresh admission fence differs')
            if source['mode'] == 'events':
                if source.get('freshReadyProof') != ready_proof:
                    raise PublicationConflictError('the frozen fresh ready proof differs')
                return
            if source['mode'] != 'admission' or source['legacyTickets'] or source['publicationRevision']:
                raise PublicationModeError('fresh admission is no longer held')
            if await self._cas(source, {'mode': 'events', 'freshReadyProof': ready_proof}):
                return
        raise PublicationConflictError('fresh source activation is busy')

    async def _archive(self, intent):
        try:
            result = await self.intents.insert_one(intent)
            if not result.acknowledged:
                raise RuntimeError('publication intent was not acknowledged')
        except DuplicateKeyError:
            current = await self.intents.find_one({'_id': intent['_id']})
            if (
                current is None
                or current['intentDigest'] != intent['intentDigest']
                or current['event'] != intent['event']
            ):
                raise PublicationConflictError('frozen publication intent conflicts') from None
            await self._clear_outbox_pending(intent)
            return current
        await self._clear_outbox_pending(intent)
        return intent

    async def _clear_outbox_pending(self, intent):
        scope = DocumentScope(intent['userId'], intent['subjectId'])
        for _ in range(32):
            source = await self.read_source(scope, intent['event']['sourceId'])
            if source is None or (source.get('currentIntent') or {}).get('_id') != intent['_id']:
                return
            if source.get('outboxPending') is False or await self._cas(source, {'outboxPending': False}):
                return
        raise PublicationConflictError('outbox archive reconciliation is busy')

    @staticmethod
    def _intent_key(scope, source_id, request_id):
        return 'document_intent_' + digest([scope.as_dict(), source_id, request_id])

    async def read_reserved(self, scope, source_id, request_id):
        return await self.read_intent(scope, self._intent_key(scope, source_id, request_id))

    async def has_current_managed_acceptance(self, scope, source_id, command_id, *, source):
        """Check the source CAS and both durable histories for an exact command."""
        intent = (source or {}).get('currentIntent') or {}
        if intent.get('_id') == self._intent_key(scope, source_id, command_id):
            return True
        if await self.read_reserved(scope, source_id, command_id) is not None:
            return True
        return await self.read_current_noop(scope, source_id, command_id) is not None

    @staticmethod
    def _current_predecessor_matches(event, expected_prior, proof):
        snapshot = event.get('snapshotRef') or {}
        return (
            event.get('eventKind') in {'publish', 'correct'}
            and event.get('publicationRevision') == expected_prior
            and snapshot.get('contractVersion') == COMPLETE_SOURCE_CONTRACT_V2
            and snapshot.get('snapshotId') == proof['priorSourceSnapshotRef']
            and event.get('eventId') == proof['priorEventId']
            and event.get('contentDigest') == proof['priorContentDigest']
        )

    @staticmethod
    def _noop_key(scope, source_id, command_id):
        return 'document_current_noop_' + digest([scope.as_dict(), source_id, command_id])

    async def _archived_noop(self, scope, source_id, command_id):
        row = await self.intents.find_one({'_id': self._noop_key(scope, source_id, command_id), **scope.as_dict()})
        if row is None:
            return None
        if (
            row.get('sourceId') != source_id
            or row.get('recoveryState') != CURRENT_NOOP_RECORD_STATE
            or row.get('proof', {}).get('commandId') != command_id
        ):
            raise PublicationConflictError('archived current no-op does not identify the accepted operation')
        return row['proof']

    async def read_current_noop(self, scope, source_id, command_id):
        source = await self.read_source(scope, source_id)
        accepted = (source or {}).get('currentNoopAcknowledgement')
        if accepted is not None and accepted['commandId'] == command_id:
            return accepted
        # The previous acknowledgement is archived before the slot is replaced.
        return await self._archived_noop(scope, source_id, command_id)

    async def _archive_noop(self, scope, source_id, proof):
        command_id = proof['commandId']
        try:
            result = await self.intents.insert_one(
                {
                    '_id': self._noop_key(scope, source_id, command_id),
                    **scope.as_dict(),
                    'sourceId': source_id,
                    'proof': proof,
                    # Excluded by all publication recovery scans and claims.
                    'recoveryState': CURRENT_NOOP_RECORD_STATE,
                }
            )
        except DuplicateKeyError:
            if await self._archived_noop(scope, source_id, command_id) != proof:
                raise PublicationConflictError('frozen current no-op acknowledgement conflicts') from None
        else:
            if not result.acknowledged:
                raise RuntimeError('current no-op archive was not acknowledged')

    async def accept_current_noop(self, scope, source_id, *, proof):
        """Accept a fixed managed command without creating a publication event."""
        if (
            not isinstance(proof, dict)
            or set(proof) != CURRENT_NOOP_PROOF_FIELDS
            or any(
                type(proof[key]) is not int or proof[key] < 1 for key in ('lifecycleEpoch', 'priorPublicationRevision')
            )
            or any(
                not isinstance(proof[key], str) or not proof[key] or proof[key] != proof[key].strip()
                for key in (
                    'commandId',
                    'priorSourceSnapshotRef',
                    'priorEventId',
                    'priorContentDigest',
                    'commandDigest',
                )
            )
        ):
            raise PublicationConflictError('an exact current no-op proof is required')
        proof = {**proof, 'interpretationRounds': self._interpretation_rounds(proof['interpretationRounds'])}
        expected_prior = proof['priorPublicationRevision']
        for _ in range(32):
            source = await self.read_source(scope, source_id)
            current_ack = (source or {}).get('currentNoopAcknowledgement')
            accepted = (
                current_ack
                if current_ack is not None and current_ack['commandId'] == proof['commandId']
                else await self._archived_noop(scope, source_id, proof['commandId'])
            )
            if accepted is not None:
                if accepted != proof:
                    raise PublicationConflictError('current no-op command was reused with changed proof')
                return accepted
            binding = (source or {}).get('currentSourceBinding')
            current = (source or {}).get('currentIntent') or {}
            if (
                binding is None
                or binding['lifecycleEpoch'] != proof['lifecycleEpoch']
                or source['mode'] != 'events'
                or source['publicationRevision'] != expected_prior
                or source['appliedRevision'] != expected_prior
                or source.get('reservedRetractionRevision', 0) >= expected_prior
                or not self._current_predecessor_matches(current.get('event') or {}, expected_prior, proof)
            ):
                raise PublicationConflictError('current no-op requires the applied predecessor and original lifecycle')
            rounds = self._advance_interpretation_rounds(source, proof['interpretationRounds'])
            if current_ack is not None:
                await self._archive_noop(scope, source_id, current_ack)
            fields = {'acceptedInterpretationRounds': rounds, 'currentNoopAcknowledgement': proof}
            if await self._cas(source, fields):
                return proof
        raise PublicationConflictError('current no-op acceptance changed concurrently')

    @classmethod
    def _check_current_lineage(cls, source, event, request_id, expected_prior, proof):
        binding = source.get('currentSourceBinding')
        contract = event['snapshotRef']['contractVersion']
        if binding is None:
            if proof is not None or contract == COMPLETE_SOURCE_CONTRACT_V2:
                raise PublicationConflictError('the original current source admission is required')
            return
        if (
            proof is None
            or proof['lifecycleEpoch'] != binding['lifecycleEpoch']
            or event['sourceRevision'] != binding['sourceRevision']
        ):
            raise PublicationConflictError('publication differs from the original current source binding')
        if event['eventKind'] == 'retract':
            if contract != RETRACTION_CONTRACT:
                raise PublicationConflictError('the current retraction contract is required')
            return
        if contract != COMPLETE_SOURCE_CONTRACT_V2:
            raise PublicationConflictError('the current source publication contract is required')
        plan_ref, transition = proof['planRef'], proof['transition']
        if event['eventKind'] == 'publish':
            if plan_ref != binding['planRef'] or transition is not None:
                raise PublicationConflictError('first publication requires the original admitted plan')
            return
        prior = (source.get('currentIntent') or {}).get('event') or {}
        if (
            event['eventKind'] != 'correct'
            or transition is None
            or plan_ref != request_id
            or transition['commandId'] != plan_ref
            or transition['priorPublicationRevision'] != expected_prior
            or not cls._current_predecessor_matches(prior, expected_prior, transition)
        ):
            raise PublicationConflictError('correction requires a verified transition from the applied predecessor')

    async def reserve(self, scope, event, request_id, expected_prior, *, current_proof=None):
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError('a permanent publication request id is required')
        if event['scope'] != scope.as_dict() or not event['sourceId']:
            raise ValueError('publication scope/source mismatch')
        if type(event['sourceRevision']) is not int or event['sourceRevision'] < 1:
            raise ValueError('invalid material source revision')
        current_proof = deepcopy(current_proof)
        key = self._intent_key(scope, event['sourceId'], request_id)
        frozen_input = [event, expected_prior]
        if current_proof is not None:
            frozen_input.append(current_proof)
        intent_digest = digest(frozen_input)
        for _ in range(32):
            existing = await self.intents.find_one({'_id': key, **scope.as_dict()})
            if existing is not None:
                if existing['intentDigest'] != intent_digest:
                    raise PublicationConflictError('publication request id was reused with changed content')
                return existing
            source = await self._source(scope, event['sourceId'])
            current = source['currentIntent']
            if current is not None and current['_id'] == key:
                if current['intentDigest'] != intent_digest:
                    raise PublicationConflictError('reserved publication input conflicts')
                return await self._archive(current)
            if source['mode'] != 'events':
                raise PublicationModeError('explicit source cutover is required before event publication')
            revision = source['publicationRevision']
            if expected_prior != (revision or None):
                raise PublicationConflictError('expected prior publication revision is stale')
            unsettled = source.get('settledRevision', source['appliedRevision']) < revision
            rescue = (
                event['eventKind'] == 'retract' and current is not None and current['event']['eventKind'] != 'retract'
            )
            if source['appliedRevision'] != revision and not rescue:
                raise PublicationConflictError('prior publication has not been applied')
            if rescue and unsettled and revision - source.get('settledRevision', source['appliedRevision']) > 1:
                raise PublicationConflictError(
                    'only one ordinary publication and its rescue retraction may be in flight'
                )
            if (event['eventKind'] == 'publish') != (revision == 0):
                raise PublicationConflictError('first event must publish; subsequent events must correct or retract')
            if (
                event['eventKind'] == 'retract'
                and current is not None
                and event['sourceRevision'] != current['event']['sourceRevision']
            ):
                raise PublicationConflictError('retraction must identify the current material revision')
            if current is not None and event['sourceRevision'] < current['event']['sourceRevision']:
                raise PublicationConflictError('material source revision cannot move backwards')
            self._check_current_lineage(source, event, request_id, expected_prior, current_proof)
            transition = current_proof.get('transition') if current_proof is not None else None
            rounds = (
                self._advance_interpretation_rounds(source, transition.get('interpretationRounds'))
                if transition is not None
                else None
            )
            if current is not None:
                # A rescue must not overwrite the only durable copy of an older
                # event when its outbox insertion previously lost acknowledgement.
                await self._archive(current)
            frozen = {
                **event,
                'publicationRevision': revision + 1,
                'priorRevision': revision or None,
                'eventId': key,
                'occurredAt': datetime.now(timezone.utc).isoformat(),
            }
            intent = {
                '_id': key,
                **scope.as_dict(),
                'intentDigest': intent_digest,
                'event': frozen,
                'receipt': None,
                'receiptVersion': 0,
                'recoveryRoute': 'current' if source.get('currentSourceBinding') is not None else 'default',
                'recoveryVersion': 0,
                'recoveryState': 'pending',
                'dispatchAttempts': 0,
                'transportFailures': 0,
            }
            fields = {'publicationRevision': revision + 1, 'currentIntent': intent, 'outboxPending': True}
            if rounds is not None:
                fields['acceptedInterpretationRounds'] = rounds
            if event['eventKind'] == 'retract':
                fields['reservedRetractionRevision'] = revision + 1
                if current is not None and current['event']['eventKind'] in {'publish', 'correct'}:
                    intent['withdrawalPredecessorId'] = current['_id']
            if await self._cas(source, fields):
                return await self._archive(intent)
        raise PublicationConflictError('publication source changed concurrently')

    async def acknowledge(self, scope, intent, receipt, *, lease=None, now=None):
        if lease is not None:
            await self.check_lease(lease, now=now)
        event = intent['event']
        if (
            receipt.source_id != event['sourceId']
            or receipt.publication_revision != event['publicationRevision']
            or receipt.content_digest != event['contentDigest']
        ):
            raise PublicationConflictError('receiver receipt does not identify the frozen publication')
        source = await self.read_source(scope, event['sourceId'])
        if source is None:
            raise PublicationConflictError('publication source is unavailable')
        self._watermarks(source, event, receipt)
        # Applied is absorbing: a delayed accepted/pending response cannot demote it.
        for _ in range(32):
            current = await self.intents.find_one({'_id': intent['_id'], **scope.as_dict()})
            if current is None:
                raise PublicationConflictError('publication intent is unavailable')
            prior = current.get('receipt')
            if prior is not None and (
                prior['state'] == 'superseded' or prior['state'] == 'applied' and receipt.state != 'superseded'
            ):
                receipt = PublicationReceipt(**prior)
                break
            version = current['receiptVersion']
            saved = await self.intents.update_one(
                {'_id': intent['_id'], 'receiptVersion': version, **(self._lease(lease, now) if lease else {})},
                {'$set': {'receipt': asdict(receipt), 'receiptVersion': version + 1}},
            )
            if not saved.acknowledged:
                raise RuntimeError('receiver receipt was not acknowledged')
            if saved.matched_count:
                break
            if lease:
                await self.check_lease(lease, now=now)
        else:
            raise PublicationConflictError('receiver receipt changed concurrently')
        for _ in range(32):
            source = await self.read_source(scope, event['sourceId'])
            fields = self._watermarks(source, event, receipt)
            if all(source.get(key) == value for key, value in fields.items()) or await self._cas(source, fields):
                return
        raise PublicationConflictError('applied watermark changed concurrently')

    async def read_intent(self, scope, intent_id):
        return await self.intents.find_one({'_id': intent_id, **scope.as_dict()})

    async def read_previous_publication(self, scope, source_id, revision, *, event_id=None):
        # New withdrawals pin the predecessor's indexed _id in the source CAS.
        # Older current withdrawals can only be read by exact source/revision.
        if event_id is not None:
            return await self.read_intent(scope, event_id)
        return await self.intents.find_one(
            {
                **scope.as_dict(),
                'event.sourceId': source_id,
                'event.publicationRevision': revision,
                'event.eventKind': {'$in': ['publish', 'correct']},
            }
        )

    async def reconcile_terminal(self, row, *, now):
        result = await self.intents.update_one(
            {
                '_id': row['_id'],
                'recoveryVersion': row.get('recoveryVersion'),
                '$or': [{'leaseUntilMs': None}, {'leaseUntilMs': {'$lte': now}}],
            },
            {'$set': {'recoveryState': row['receipt']['state']}},
        )
        if not result.acknowledged:
            raise RuntimeError('terminal recovery acknowledgement was not acknowledged')

    async def repair_outbox(self, *, limit, after_id=None, recovery_route=None):
        query = {'mode': 'events', 'outboxPending': {'$in': [True, None]}}
        if recovery_route is not None:
            query['currentSourceBinding'] = {'$exists': recovery_route == 'current'}
        if after_id is not None:
            query['_id'] = {'$gt': after_id}
        rows = await self.sources.find(query).sort([('_id', 1)]).to_list(length=limit + 1)
        repaired = 0
        for source in rows[:limit]:
            intent = source.get('currentIntent')
            if intent is None:
                continue
            try:
                await self._archive(intent)
                repaired += 1
            except Exception:
                # The retained source intent remains the recovery authority.
                continue
        more = len(rows) > limit
        return repaired, more, rows[limit - 1]['_id'] if more else None

    async def scan_pending(self, *, now, limit, after_id=None, recovery_route=None):
        query = {
            'recoveryState': {'$in': [None, 'pending', 'processing', 'polling']},
            '$or': [{'nextAttemptAtMs': None}, {'nextAttemptAtMs': {'$lte': now}}],
        }
        if recovery_route is not None:
            query.update(
                {
                    'recoveryRoute': {'$in': [recovery_route, None]},
                    'event.sourceType': 'health_report',
                    'event.snapshotRef.provider': 'document',
                    'event.snapshotRef.contractVersion': {
                        '$in': [RECOVERY_MATERIAL_CONTRACTS[recovery_route], RETRACTION_CONTRACT],
                    },
                }
            )
        if after_id is not None:
            query['_id'] = {'$gt': after_id}
        rows = await self.intents.find(query).sort([('_id', 1)]).to_list(length=limit + 1)
        return rows[:limit], len(rows) > limit

    @staticmethod
    def _lease(row, now):
        return {
            '_id': row['_id'],
            'userId': row['userId'],
            'subjectId': row['subjectId'],
            'recoveryToken': row['recoveryToken'],
            'leaseUntilMs': {'$gt': now},
        }

    async def check_lease(self, row, *, now):
        current = await self.intents.find_one(self._lease(row, now))
        if current is None:
            raise PublicationLeaseLost('publication recovery lease is no longer owned')
        return current

    async def claim_intent(self, scope, intent_id, *, now, policy):
        for _ in range(8):
            current = await self.read_intent(scope, intent_id)
            if current is None:
                raise PublicationConflictError('publication intent is unavailable in scope')
            if current.get('recoveryState', 'pending') not in {'pending', 'processing', 'polling'}:
                return None
            if (current.get('leaseUntilMs') or 0) > now:
                return None
            policy = PublicationRecoveryPolicy(**current['recoveryPolicy']) if current.get('recoveryPolicy') else policy
            created = int(datetime.fromisoformat(current['event']['occurredAt']).timestamp() * 1000)
            deadline = current.get('recoveryDeadlineMs', created + policy.deadline_seconds * 1000)
            fields = {
                'recoveryVersion': current.get('recoveryVersion', 0) + 1,
                'recoveryDeadlineMs': deadline,
                'recoveryPolicy': asdict(policy),
                'recoveryState': 'stalled' if now >= deadline else 'processing',
                'recoveryError': 'document.publication_deadline_exceeded' if now >= deadline else None,
                'recoveryToken': uuid4().hex,
                'leaseUntilMs': now + policy.lease_seconds * 1000 if now < deadline else 0,
                'nextAttemptAtMs': now + policy.lease_seconds * 1000,
            }
            result = await self.intents.update_one(
                {'_id': intent_id, **scope.as_dict(), 'recoveryVersion': current.get('recoveryVersion')},
                {'$set': fields},
            )
            if not result.acknowledged:
                raise RuntimeError('publication claim was not acknowledged')
            if result.matched_count:
                return {**current, **fields} if now < deadline else None
        raise PublicationConflictError('publication recovery claim is busy')

    async def before_dispatch(self, row, *, now, policy):
        current = await self.check_lease(row, now=now)
        policy = PublicationRecoveryPolicy(**current['recoveryPolicy'])
        if now >= current['recoveryDeadlineMs']:
            raise PublicationRecoveryError('document.publication_deadline_exceeded')
        if current.get('dispatchesSinceRedrive', 0) >= policy.max_dispatches:
            raise PublicationRecoveryError('document.publication_dispatch_budget_exceeded')
        fields = {
            'dispatchAttempts': current.get('dispatchAttempts', 0) + 1,
            'dispatchesSinceRedrive': current.get('dispatchesSinceRedrive', 0) + 1,
        }
        result = await self.intents.update_one(self._lease(row, now), {'$set': fields})
        if not result.acknowledged:
            raise RuntimeError('dispatch reservation was not acknowledged')
        if not result.matched_count:
            raise PublicationLeaseLost('publication dispatch lease was lost')
        return {**current, **fields}

    async def finish_attempt(self, row, *, now, state, delay_ms=0, error_code=None):
        result = await self.intents.update_one(
            self._lease(row, now),
            {
                '$set': {
                    'recoveryState': state,
                    'recoveryError': error_code,
                    'leaseUntilMs': 0,
                    'nextAttemptAtMs': now + delay_ms,
                }
            },
        )
        if not result.acknowledged:
            raise RuntimeError('publication attempt completion was not acknowledged')
        if not result.matched_count:
            raise PublicationLeaseLost('publication completion lease was lost')

    async def fail_attempt(self, row, *, now, policy, error_code):
        current = await self.check_lease(row, now=now)
        policy = PublicationRecoveryPolicy(**current['recoveryPolicy'])
        failures = current.get('transportFailures', 0) + 1
        since_redrive = current.get('failuresSinceRedrive', 0) + 1
        stalled = (
            since_redrive >= policy.max_transport_failures
            or current.get('dispatchesSinceRedrive', 0) >= policy.max_dispatches
            or now >= current['recoveryDeadlineMs']
        )
        result = await self.intents.update_one(
            self._lease(row, now),
            {
                '$set': {
                    'transportFailures': failures,
                    'failuresSinceRedrive': since_redrive,
                    'recoveryState': 'stalled' if stalled else 'pending',
                    'recoveryError': error_code,
                    'leaseUntilMs': 0,
                    'nextAttemptAtMs': now + policy.poll_seconds * 1000,
                }
            },
        )
        if not result.acknowledged:
            raise RuntimeError('publication retry reservation was not acknowledged')
        if not result.matched_count:
            raise PublicationLeaseLost('publication failure lease was lost')

    async def redrive_intent(self, scope, intent_id, *, now, policy, reason):
        current = await self.read_intent(scope, intent_id)
        if current is None:
            raise PublicationConflictError('publication intent is unavailable in scope')
        if current.get('recoveryState') not in {'stalled', 'failed', 'partial'}:
            raise PublicationRecoveryError('only stalled/failed/partial publication recovery can be redriven')
        if (current.get('leaseUntilMs') or 0) > now:
            raise PublicationLeaseLost('publication recovery is still leased')
        result = await self.intents.update_one(
            {'_id': intent_id, **scope.as_dict(), 'recoveryVersion': current.get('recoveryVersion')},
            {
                '$set': {
                    'recoveryVersion': current.get('recoveryVersion', 0) + 1,
                    'recoveryState': 'pending',
                    'recoveryPolicy': asdict(policy),
                    'previousRecoveryPolicy': current.get('recoveryPolicy'),
                    'recoveryDeadlineMs': now + policy.deadline_seconds * 1000,
                    'dispatchesSinceRedrive': 0,
                    'failuresSinceRedrive': 0,
                    'nextAttemptAtMs': now,
                    'lastRedriveReason': reason,
                    'redriveCount': current.get('redriveCount', 0) + 1,
                }
            },
        )
        if not result.acknowledged:
            raise RuntimeError('publication redrive was not acknowledged')
        if not result.matched_count:
            raise PublicationConflictError('publication recovery changed while redriving')

    @staticmethod
    def _watermarks(source, event, receipt):
        accepted = receipt.accepted_revision if receipt.accepted_revision is not None else event['publicationRevision']
        applied = (
            receipt.applied_revision
            if receipt.applied_revision is not None
            else (event['publicationRevision'] if receipt.state == 'applied' else 0)
        )
        settled = receipt.settled_revision if receipt.settled_revision is not None else applied
        accepted_retraction = max(
            receipt.accepted_retraction_revision,
            event['publicationRevision'] if event['eventKind'] == 'retract' else 0,
        )
        applied_retraction = max(
            receipt.applied_retraction_revision,
            event['publicationRevision'] if event['eventKind'] == 'retract' and receipt.state == 'applied' else 0,
        )
        if not (
            0 <= applied <= settled <= accepted <= source['publicationRevision']
            and 0 <= applied_retraction <= accepted_retraction <= accepted
            and applied_retraction <= applied
            and accepted >= event['publicationRevision']
        ):
            raise PublicationConflictError('receiver publication watermarks are inconsistent')
        if receipt.state == 'applied' and applied < event['publicationRevision']:
            raise PublicationConflictError('applied receipt has no covering application watermark')
        if event['eventKind'] == 'retract' and receipt.accepted_revision is not None:
            if receipt.accepted_retraction_revision < event['publicationRevision']:
                raise PublicationConflictError('accepted retraction watermark does not cover this event')
            if receipt.state == 'applied' and receipt.applied_retraction_revision < event['publicationRevision']:
                raise PublicationConflictError('applied retraction watermark does not cover this event')
        return {
            'acceptedRevision': max(source.get('acceptedRevision', source['appliedRevision']), accepted),
            'appliedRevision': max(source['appliedRevision'], applied),
            'settledRevision': max(source.get('settledRevision', source['appliedRevision']), settled),
            'acceptedRetractionRevision': max(source.get('acceptedRetractionRevision', 0), accepted_retraction),
            'appliedRetractionRevision': max(source.get('appliedRetractionRevision', 0), applied_retraction),
        }
