"""Single auditable registry for every public Tornado endpoint."""

from __future__ import annotations

import tornado.web

from bootstrap.container import AppContainer
from web.handlers import ChatCustomMessageHandler
from web.handlers import ChatHistoryHandler
from web.handlers import ChatSendCompleteHandler
from web.handlers import ChatSendStreamHandler
from web.handlers import ChatSuggestionsHandler
from web.handlers import HealthCheckHandler
from web.handlers import HealthProfileChangeListHandler
from web.handlers import HealthProfileDocumentObservationListHandler
from web.handlers import HealthProfileFactDeleteHandler
from web.handlers import HealthProfileFactListHandler
from web.handlers import HealthProfileFactResolveHandler
from web.handlers import HealthProfileGetHandler
from web.handlers import HealthProfileSubjectListHandler
from web.handlers import HealthProfileSubjectUpsertHandler
from web.handlers import HealthReportHandler
from web.handlers import LivenessHandler
from web.handlers import QASessionAnswerHandler
from web.handlers import QASessionAnswerStreamHandler
from web.handlers import QASessionCompleteHandler
from web.handlers import QASessionContinueHandler
from web.handlers import QASessionCreateHandler
from web.handlers import QASessionGetHandler
from web.handlers import QASessionHistoryHandler
from web.handlers import ReadinessHandler
from web.handlers import ReportGenerateHandler
from web.handlers import ReportGetHandler
from web.handlers import ReportListHandler
from web.handlers.gut_barrier import GutBarrierGenerateHandler
from web.handlers.health_analysis import HealthAnalysisHandler
from web.handlers.questionnaire import QuestionnaireAnswersHandler
from web.handlers.questionnaire import QuestionnaireCollectionHandler
from web.handlers.questionnaire import QuestionnaireFactorAssessmentHandler


def build_routes(container: AppContainer) -> list[tornado.web.URLSpec]:
    """Return the complete route table with explicit service injection."""
    record = {'service': container.record_service}
    chat = {'service': container.chat_service}
    profile = {'service': container.health_profile_service}
    analysis = {
        'processing_service_factory': container.analysis_service_factory,
    }
    report_query = {'service': container.report_artifact_store}
    gut_barrier = {'service': container.gut_barrier_service, 'task_supervisor': container.task_supervisor}
    report = {
        'pipeline': container.report_pipeline,
        'task_supervisor': container.task_supervisor,
        'task_coordinator': container.report_task_coordinator,
    }
    if container.report_service is not None:
        report['service'] = container.report_service
    health_analysis = {'service': container.health_analysis_service}
    return [
        tornado.web.URLSpec(
            r'/api/v1/health-analysis',
            HealthAnalysisHandler,
            health_analysis,
            name='health-analysis.generate',
        ),
        tornado.web.URLSpec(
            r'/api/v1/record/questionnaires',
            QuestionnaireCollectionHandler,
            {'service': container.questionnaire_service},
            name='questionnaire.collection',
        ),
        tornado.web.URLSpec(
            r'/api/v1/record/questionnaires/([^/]+)/answers',
            QuestionnaireAnswersHandler,
            {'service': container.questionnaire_service},
            name='questionnaire.submit',
        ),
        tornado.web.URLSpec(
            r'/api/v1/record/questionnaires/([^/]+)/factor-assessment',
            QuestionnaireFactorAssessmentHandler,
            {'service': container.questionnaire_service},
            name='questionnaire.factor-assessment',
        ),
        tornado.web.URLSpec(r'/health', HealthCheckHandler, name='health'),
        tornado.web.URLSpec(
            r'/health/live',
            LivenessHandler,
            name='health.live',
        ),
        tornado.web.URLSpec(
            r'/health/ready',
            ReadinessHandler,
            name='health.ready',
        ),
        tornado.web.URLSpec(
            r'/api/v1/interpret/health-report',
            HealthReportHandler,
            analysis,
            name='analysis.health-report',
        ),
        tornado.web.URLSpec(
            r'/api/v1/qa/create',
            QASessionCreateHandler,
            record,
            name='record.create',
        ),
        tornado.web.URLSpec(
            r'/api/v1/qa/continue',
            QASessionContinueHandler,
            record,
            name='record.continue',
        ),
        tornado.web.URLSpec(
            r'/api/v1/qa/complete',
            QASessionCompleteHandler,
            record,
            name='record.complete',
        ),
        tornado.web.URLSpec(
            r'/api/v1/qa/answer/complete',
            QASessionAnswerHandler,
            record,
            name='record.answer.complete',
        ),
        tornado.web.URLSpec(
            r'/api/v1/qa/answer/stream',
            QASessionAnswerStreamHandler,
            record,
            name='record.answer.stream',
        ),
        tornado.web.URLSpec(
            r'/api/v1/qa/status',
            QASessionGetHandler,
            record,
            name='record.status',
        ),
        tornado.web.URLSpec(
            r'/api/v1/qa/history',
            QASessionHistoryHandler,
            record,
            name='record.history',
        ),
        tornado.web.URLSpec(
            r'/api/v1/chat/suggestions',
            ChatSuggestionsHandler,
            chat,
            name='chat.suggestions',
        ),
        tornado.web.URLSpec(
            r'/api/v1/chat/messages/custom',
            ChatCustomMessageHandler,
            chat,
            name='chat.messages.custom',
        ),
        tornado.web.URLSpec(
            r'/api/v1/chat/send/complete',
            ChatSendCompleteHandler,
            chat,
            name='chat.send.complete',
        ),
        tornado.web.URLSpec(
            r'/api/v1/chat/send/stream',
            ChatSendStreamHandler,
            chat,
            name='chat.send.stream',
        ),
        tornado.web.URLSpec(
            r'/api/v1/chat/history',
            ChatHistoryHandler,
            chat,
            name='chat.history',
        ),
        tornado.web.URLSpec(
            r'/api/v1/health-profile/get',
            HealthProfileGetHandler,
            profile,
            name='health-profile.get',
        ),
        tornado.web.URLSpec(
            r'/api/v1/health-profile/subjects/list',
            HealthProfileSubjectListHandler,
            profile,
            name='health-profile.subjects.list',
        ),
        tornado.web.URLSpec(
            r'/api/v1/health-profile/document-observations/list',
            HealthProfileDocumentObservationListHandler,
            profile,
            name='health-profile.document-observations.list',
        ),
        tornado.web.URLSpec(
            r'/api/v1/health-profile/subjects/upsert',
            HealthProfileSubjectUpsertHandler,
            profile,
            name='health-profile.subjects.upsert',
        ),
        tornado.web.URLSpec(
            r'/api/v1/health-profile/facts/list',
            HealthProfileFactListHandler,
            profile,
            name='health-profile.facts.list',
        ),
        tornado.web.URLSpec(
            r'/api/v1/health-profile/facts/resolve',
            HealthProfileFactResolveHandler,
            profile,
            name='health-profile.facts.resolve',
        ),
        tornado.web.URLSpec(
            r'/api/v1/health-profile/facts/delete',
            HealthProfileFactDeleteHandler,
            profile,
            name='health-profile.facts.delete',
        ),
        tornado.web.URLSpec(
            r'/api/v1/health-profile/changes/list',
            HealthProfileChangeListHandler,
            profile,
            name='health-profile.changes.list',
        ),
        tornado.web.URLSpec(
            r'/api/v1/report',
            ReportGenerateHandler,
            report,
            name='report.submit',
        ),
        tornado.web.URLSpec(
            r'/api/v1/report/list',
            ReportListHandler,
            report_query,
            name='report.list',
        ),
        tornado.web.URLSpec(
            r'/api/v1/report/get',
            ReportGetHandler,
            report_query,
            name='report.get',
        ),
        tornado.web.URLSpec(
            r'/api/v1/gut-barrier/report',
            GutBarrierGenerateHandler,
            gut_barrier,
            name='gut-barrier.submit',
        ),
    ]


__all__ = ['build_routes']
