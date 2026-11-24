"""Durable outbox delivery for immutable whole-questionnaire submissions."""

from utils.log import getLogger

from .models import ProfileIngestionSourceType

logger = getLogger(__name__)


def _allows_legacy_delivery(snapshot):
    if not isinstance(snapshot, dict):
        return False
    ingestion = snapshot.get('profileIngestion', {})
    if not isinstance(ingestion, dict):
        return False
    mode = ingestion.get('deliveryMode')
    return mode is None or (isinstance(mode, str) and mode == 'legacy')


class QuestionnaireProfileSourceService:
    def __init__(self, store, job_queue):
        self._store = store
        self._job_queue = job_queue

    async def ensure_queued(self, *, user_id, questionnaire_id):
        snapshot = await self._store.get_submitted(user_id, questionnaire_id)
        if snapshot is None or not _allows_legacy_delivery(snapshot):
            # The owner mode is authoritative before any Profile marker exists.
            # A queued status is not permission to resume an unsupported sender.
            return None
        if snapshot.get('profileIngestion', {}).get('status') == 'queued':
            return snapshot
        try:
            result = await self._job_queue.enqueue(
                source_type=ProfileIngestionSourceType.QUESTIONNAIRE,
                source_id=questionnaire_id,
                source_revision=snapshot['sourceRevision'],
                user_id=user_id,
                subject_id=None,
                idempotency_key=f'questionnaire:{questionnaire_id}:{snapshot["sourceRevision"]}',
            )
            if not await self._store.mark_ingestion_queued(user_id, questionnaire_id, result.job.job_id):
                raise RuntimeError('questionnaire outbox update failed')
            return snapshot
        except Exception as error:
            logger.error(
                'questionnaire profile delivery failed: questionnaire_id=%s error_type=%s',
                questionnaire_id,
                type(error).__name__,
            )
            return None

    async def recover_pending(self, *, limit=100):
        pending = await self._store.list_pending_ingestion(limit=limit)
        recovered = 0
        for item in pending:
            if not _allows_legacy_delivery(item):
                continue
            if await self.ensure_queued(user_id=item['userId'], questionnaire_id=item['questionnaireId']) is not None:
                recovered += 1
        return recovered
