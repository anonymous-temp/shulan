"""Two-stage Questionnaire source assembly; default installation belongs to the root."""

from dataclasses import dataclass

from pymongo import ReadPreference
from pymongo.write_concern import WriteConcern

from adapters.analysis.questionnaire_publication import create_document_questionnaire_attachment_publisher
from adapters.questionnaire_profile_source import QuestionnaireDocumentAttachmentAdapter
from adapters.questionnaire_profile_source import QuestionnaireSourceResources
from adapters.questionnaire_profile_source import create_questionnaire_profile_source
from adapters.questionnaire_profile_source import create_questionnaire_source_resources
from modules.questionnaire.profile_source import QuestionnaireSourcePolicy


@dataclass(frozen=True)
class QuestionnaireSourceCompositionResources:
    """Permanent source data and a separate Document attachment-command namespace."""

    sources: QuestionnaireSourceResources
    attachment_commands: object

    @property
    def snapshot_reader(self):
        return self.sources.snapshot_reader

    @property
    def fence_reader(self):
        return self.sources.fence_reader


def build_questionnaire_source_resources(*, questionnaire_collection):
    """Create readers before Profile, using the configured Questionnaire database.

    Construction does not perform I/O, create indexes/TTL, start workers or
    authorize a source. Deployment retains the built-in unique _id indexes.
    """
    database, prefix = questionnaire_collection.database, questionnaire_collection.name
    options = {
        'read_preference': ReadPreference.PRIMARY,
        'write_concern': WriteConcern(w='majority', j=True, wtimeout=5000),
    }
    sources = create_questionnaire_source_resources(
        collection=database[prefix + '_profile_sources'].with_options(**options),
        questionnaires=questionnaire_collection,
    )
    return QuestionnaireSourceCompositionResources(
        sources=sources,
        attachment_commands=database[prefix + '_document_publications'].with_options(**options),
    )


def build_questionnaire_source_delivery(
    resources,
    *,
    inbox,
    presence,
    fresh_admission,
    submitted_reader,
    health_report_source,
    publication_ledger,
    policy=QuestionnaireSourcePolicy(),
    clock=None,
):
    """Wire existing public providers after Profile has registered the early readers.

    The caller supplies the same Document ledger and legacy source used by its
    other writers. Only the Questionnaire router recovers attachment commands;
    this helper creates neither a second attachment worker nor a new Profile.
    """
    if not isinstance(resources, QuestionnaireSourceCompositionResources):
        raise TypeError('the first-stage Questionnaire source resources are required')
    publisher = create_document_questionnaire_attachment_publisher(
        submitted_reader=submitted_reader,
        source_reader=resources.snapshot_reader,
        profile_inbox=inbox,
        health_report_source=health_report_source,
        publication_ledger=publication_ledger,
        command_records=resources.attachment_commands,
    )
    return create_questionnaire_profile_source(
        resources=resources.sources,
        inbox=inbox,
        presence=presence,
        fresh_admission=fresh_admission,
        submitted=submitted_reader,
        attachments=QuestionnaireDocumentAttachmentAdapter(publisher),
        policy=policy,
        clock=clock,
    )
