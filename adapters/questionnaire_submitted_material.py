"""Primary-only owner reads for public committed Questionnaire material."""

from pymongo import ReadPreference

from adapters.persistence.questionnaires import MongoQuestionnaireStore
from adapters.questionnaire_documents import MongoQuestionnaireArchive
from modules.questionnaire.submitted_material import QuestionnaireSubmittedMaterialReader


def create_questionnaire_submitted_material_reader(*, questionnaires, documents, chunks):
    return QuestionnaireSubmittedMaterialReader(
        store=MongoQuestionnaireStore(questionnaires.with_options(read_preference=ReadPreference.PRIMARY)),
        archive=MongoQuestionnaireArchive(
            documents.with_options(read_preference=ReadPreference.PRIMARY),
            chunks.with_options(read_preference=ReadPreference.PRIMARY),
        ),
    )


def create_application_questionnaire_submitted_material_reader():
    from db.mongodb import collection_questionnaire_chunks
    from db.mongodb import collection_questionnaire_documents
    from db.mongodb import collection_questionnaires

    return create_questionnaire_submitted_material_reader(
        questionnaires=collection_questionnaires,
        documents=collection_questionnaire_documents,
        chunks=collection_questionnaire_chunks,
    )
