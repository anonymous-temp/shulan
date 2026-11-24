"""Composition of the independent questionnaire domain; no eager database I/O."""


def create_questionnaire_store():
    from adapters.persistence.questionnaires import MongoQuestionnaireStore
    from db.mongodb import collection_questionnaires
    from db.redis import client

    return MongoQuestionnaireStore(collection_questionnaires, cache=client)


def create_questionnaire_archive():
    from adapters.questionnaire_documents import MongoQuestionnaireArchive
    from db.mongodb import collection_questionnaire_chunks
    from db.mongodb import collection_questionnaire_documents

    return MongoQuestionnaireArchive(collection_questionnaire_documents, collection_questionnaire_chunks)


def create_questionnaire_report_reader():
    from adapters.questionnaire_documents import QuestionnaireDocumentStager

    return QuestionnaireDocumentStager(interpreter=None, archive=create_questionnaire_archive())


def build_questionnaire_service(
    *,
    runtime,
    interpreter,
    session_repository,
    session_cache,
    task_supervisor,
    profile_source_guard=None,
    profile_source_delivery=None,
):
    from adapters.analysis.document_trace import create_application_document_trace_store
    from adapters.persistence.questionnaire_factor_tasks import MongoQuestionnaireFactorTaskStore
    from adapters.questionnaire_documents import MongoOwnedQuestionnaireDocumentReader
    from adapters.questionnaire_documents import QuestionnaireDocumentStager
    from adapters.questionnaire_evidence import create_questionnaire_evidence_policy
    from adapters.questionnaire_factors import QuestionnaireFactorModel
    from adapters.questionnaire_generation import QuestionnairePlanGateway
    from db.mongodb import collection_document_index
    from db.mongodb import collection_health_subjects
    from db.mongodb import collection_questionnaire_factor_tasks
    from health_profile.application import create_application_questionnaire_profile_source
    from modules.questionnaire.agent import questionnaire_agent_manifest
    from modules.questionnaire.evidence import LEGACY_EVIDENCE_RULE_SELECTION
    from modules.questionnaire.factor_tasks import QuestionnaireFactorTasks
    from modules.questionnaire.factors import QuestionnaireFactorPipeline
    from modules.questionnaire.generation import QuestionnaireGenerator
    from modules.questionnaire.service import QuestionnaireService

    store = create_questionnaire_store()
    source = create_application_questionnaire_profile_source(store=store)
    factors = QuestionnaireFactorPipeline(
        model=QuestionnaireFactorModel(llm_gateway=runtime.llm_gateway),
        knowledge=runtime.knowledge_gateway,
        evidence_policy=create_questionnaire_evidence_policy(selection=LEGACY_EVIDENCE_RULE_SELECTION),
    )
    factor_tasks = QuestionnaireFactorTasks(
        store=MongoQuestionnaireFactorTaskStore(collection_questionnaire_factor_tasks),
        questionnaire_store=store,
        evaluator=factors,
    )
    documents = QuestionnaireDocumentStager(
        interpreter=interpreter,
        archive=create_questionnaire_archive(),
        source_reader=MongoOwnedQuestionnaireDocumentReader(
            document_index=collection_document_index,
            document_traces=create_application_document_trace_store(),
            subjects=collection_health_subjects,
        ),
    )
    generator = QuestionnaireGenerator(
        documents=documents,
        planner=QuestionnairePlanGateway(llm_gateway=runtime.llm_gateway),
        factor_pipeline=factors,
    )

    def manifest():
        return {
            **questionnaire_agent_manifest(),
            'generation': generator.VERSION,
            'reportPlan': generator.planner.VERSION,
            'factorPipeline': factors.VERSION,
            'factorModel': factors.model.VERSION,
            'evidenceRuleSelection': factors.evidence_rule_selection(),
        }

    return QuestionnaireService(
        store=store,
        generator=generator,
        manifest_provider=manifest,
        lease_manager=runtime.lease_manager,
        session_repository=session_repository,
        legacy_session_cache=session_cache,
        profile_delivery=source.ensure_queued,
        profile_source_guard=profile_source_guard,
        profile_source_delivery=profile_source_delivery,
        factor_tasks=factor_tasks,
        task_spawner=lambda coroutine: task_supervisor.create_task(coroutine, name='questionnaire-generation'),
    )
