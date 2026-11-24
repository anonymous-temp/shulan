"""Pure, exact-version reads of committed Questionnaire attachment evidence."""

from __future__ import annotations

from copy import deepcopy
from typing import Protocol

from .schemas import stable_hash

SUBMITTED_MATERIAL_SELECTION_V1 = {
    'contractVersion': 'questionnaire.submitted-material.v1',
    'implementationRevision': 'questionnaire-submitted-material-v1',
}


class QuestionnaireSubmittedMaterialError(ValueError):
    """The requested committed evidence is unsupported or no longer matches."""


class QuestionnaireSubmittedMaterialPort(Protocol):
    async def read_submission(
        self, *, user_id: str, questionnaire_id: str, source_revision: int, selection: dict
    ) -> dict: ...
    async def read_snapshot(self, reference: dict, *, user_id: str) -> dict: ...


def _text(value, field):
    if not isinstance(value, str) or not value or value.strip() != value:
        raise QuestionnaireSubmittedMaterialError(f'Invalid submitted material {field}')
    return value


def _selection(value):
    if value != SUBMITTED_MATERIAL_SELECTION_V1:
        raise QuestionnaireSubmittedMaterialError('Unsupported Questionnaire submitted material selection')


class QuestionnaireSubmittedMaterialReader:
    """Read existing retained owner data; never stage, pin, generate or publish."""

    def __init__(self, *, store, archive):
        self.store, self.archive = store, archive

    async def read_submission(self, *, user_id, questionnaire_id, source_revision, selection):
        _selection(selection)
        _text(user_id, 'userId')
        _text(questionnaire_id, 'questionnaireId')
        if type(source_revision) is not int or source_revision != 1:
            raise QuestionnaireSubmittedMaterialError('Unsupported Questionnaire submitted source revision')
        source = await self.store.get_submitted(user_id, questionnaire_id)
        if source is None:
            raise LookupError('Submitted Questionnaire material is unavailable')
        if (
            source.get('userId') != user_id
            or source.get('questionnaireId') != questionnaire_id
            or source.get('submissionId') != questionnaire_id
            or source.get('status') != 'submitted'
            or type(source.get('sourceRevision')) is not int
            or source['sourceRevision'] != source_revision
        ):
            raise QuestionnaireSubmittedMaterialError('Questionnaire submission ownership or revision changed')
        _text(source.get('submittedAt'), 'submittedAt')
        _text(source.get('sessionId'), 'sessionId')
        ingestion = source.get('profileIngestion', {})
        if not isinstance(ingestion, dict):
            raise QuestionnaireSubmittedMaterialError('Invalid Questionnaire source delivery metadata')
        delivery_mode = ingestion.get('deliveryMode')
        if delivery_mode not in (None, 'legacy', 'profile-source-event.v1'):
            raise QuestionnaireSubmittedMaterialError('Unsupported Questionnaire source delivery mode')
        reports, artifacts = source.get('reports', []), source.get('artifacts', [])
        if not isinstance(reports, list) or not isinstance(artifacts, list):
            raise QuestionnaireSubmittedMaterialError('Invalid committed Questionnaire attachment inventory')
        successful = {}
        for report in reports:
            if not isinstance(report, dict):
                raise QuestionnaireSubmittedMaterialError('Invalid committed Questionnaire report')
            if isinstance(report.get('interpretation'), dict):
                artifact_id = _text(report.get('artifactId'), 'artifactId')
                if artifact_id in successful:
                    raise QuestionnaireSubmittedMaterialError('Duplicate committed Questionnaire report identity')
                successful[artifact_id] = report['interpretation']
        artifact_ids = [_text(item.get('artifactId'), 'artifactId') for item in artifacts if isinstance(item, dict)]
        if len(artifact_ids) != len(artifacts) or len(set(artifact_ids)) != len(artifact_ids):
            raise QuestionnaireSubmittedMaterialError('Duplicate or invalid Questionnaire artifact inventory')
        if not set(successful).issubset(artifact_ids):
            raise QuestionnaireSubmittedMaterialError('Committed Questionnaire report has no owned artifact')
        selected, excluded, seen = [], [], set()
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise QuestionnaireSubmittedMaterialError('Invalid committed Questionnaire artifact')
            artifact_id = _text(artifact.get('artifactId'), 'artifactId')
            if artifact.get('questionnaireId') != questionnaire_id:
                raise QuestionnaireSubmittedMaterialError('Questionnaire artifact belongs to another submission')
            if artifact.get('kind') == 'document_reference' or artifact_id not in successful:
                excluded.append(
                    {
                        'artifactId': artifact_id,
                        'reason': 'document_reference'
                        if artifact.get('kind') == 'document_reference'
                        else 'no_interpretation',
                    }
                )
                continue
            # Preserve the legacy source identity: a missing SHA uses the artifact id,
            # even if newer archive metadata could supply a SHA on a later read.
            content_key = artifact.get('contentSha256') or artifact_id
            _text(content_key, 'contentKey')
            if content_key in seen:
                excluded.append({'artifactId': artifact_id, 'reason': 'duplicate_content'})
                continue
            record = await self.archive.get(user_id=user_id, artifact_id=artifact_id)
            if (
                record is None
                or record.get('userId') != user_id
                or record.get('questionnaireId') != questionnaire_id
                or record.get('retained') is not True
                or record.get('cleanupState')
            ):
                raise LookupError('Submitted Questionnaire archive is unavailable or not retained')
            if artifact.get('contentSha256') and record.get('contentSha256') != artifact['contentSha256']:
                raise QuestionnaireSubmittedMaterialError('Questionnaire archive content identity changed')
            interpretation = await self.archive.read_payload(artifact_id, user_id, None, 'interpretation')
            if not isinstance(interpretation, dict) or interpretation != successful[artifact_id]:
                raise QuestionnaireSubmittedMaterialError('Questionnaire archive differs from committed interpretation')
            selected.append(
                {
                    'artifactId': artifact_id,
                    'contentKey': content_key,
                    'artifact': deepcopy(artifact),
                    'interpretation': deepcopy(interpretation),
                    'interpretationDigest': stable_hash(interpretation),
                    'interpretationContractVersion': 'questionnaire-archived-interpretation.v1',
                }
            )
            seen.add(content_key)
        proof = {
            'contractVersion': 'questionnaire-submission-proof.v1',
            'status': 'submitted',
            'deliveryMode': delivery_mode,
            'userId': user_id,
            'questionnaireId': questionnaire_id,
            'submissionId': source['submissionId'],
            'sessionId': source.get('sessionId'),
            'sourceRevision': source_revision,
            'submittedAt': source['submittedAt'],
            'answerHash': source.get('answerHash'),
            'rawAnswerHash': source.get('rawAnswerHash'),
            'attachmentInventoryDigest': stable_hash({'reports': reports, 'artifacts': artifacts}),
        }
        payload = {
            **deepcopy(SUBMITTED_MATERIAL_SELECTION_V1),
            'provider': 'questionnaire',
            'submissionProof': proof,
            'attachments': selected,
            'excluded': excluded,
        }
        reference = {
            'provider': 'questionnaire',
            **deepcopy(SUBMITTED_MATERIAL_SELECTION_V1),
            'questionnaireId': questionnaire_id,
            'sourceRevision': source_revision,
            'snapshotId': 'qsubmitted_' + stable_hash(payload),
        }
        return {'reference': reference, 'contentDigest': stable_hash(payload), 'payload': payload}

    async def read_snapshot(self, reference, *, user_id):
        if not isinstance(reference, dict) or reference.get('provider') != 'questionnaire':
            raise QuestionnaireSubmittedMaterialError('Unsupported Questionnaire submitted material reference')
        reference = deepcopy(reference)
        result = await self.read_submission(
            user_id=user_id,
            questionnaire_id=reference.get('questionnaireId'),
            source_revision=reference.get('sourceRevision'),
            selection={key: reference.get(key) for key in SUBMITTED_MATERIAL_SELECTION_V1},
        )
        if result['reference'] != reference:
            raise QuestionnaireSubmittedMaterialError('Fixed Questionnaire submitted material no longer matches')
        return result
