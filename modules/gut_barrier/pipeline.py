"""One immutable clinical document drives JSON and HTML."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from datetime import date

from .assessment import assess_case
from .content_catalog import CONTENT_VERSION
from .content_catalog import TEMPLATE_VERSION
from .display_versions import LATEST_DISPLAY_TEMPLATE
from .dynamic_diseases import apply_dynamic_assessment
from .dynamic_diseases import evidence_queries
from .interventions import build_interventions
from .presentation import build_report
from .reference_priority import reference_selection_audit
from .review_release import REVIEW_VERSION
from .review_release import apply_review_release
from .risk_evaluator import evaluate_registered_risks
from .risk_index_rules import VERSION as RISK_RULE_VERSION
from .risk_index_rules import apply_risk_index_rules
from .risk_model import build_disease_risks
from .rule_tools import RULE_TOOL_VERSIONS
from .rule_tools import GutBarrierRuleTools
from .rule_tools import RuleBinding
from .source_scope import source_scope_block


def _incomplete_risk_results(assessment):
    """Current quotas require the full rule catalog; retain older model checks."""
    ledger = assessment.get('riskRuleAssessment', {})
    items = ledger.get('items', [])
    if not items:
        return True
    if 'v2.7' in RISK_RULE_VERSION and ledger.get('version') == RISK_RULE_VERSION:
        expected = {row['code'] for row in build_disease_risks()[0]}
        return (
            len(items) != len(expected)
            or {item['code'] for item in items} != expected
            or any(item.get('status') != 'evaluated' for item in items)
        )
    pending = {item['code'] for item in items if item.get('status') != 'evaluated'}
    if not pending:
        return False
    model = assessment.get('riskModel', {})
    if model.get('status') not in {'evaluated', 'partial'} or not model.get('modelId') or not model.get('modelVersion'):
        return True
    audited = {
        item['code'] for item in model.get('items', []) if item.get('status') == 'evaluated' and not item.get('reasons')
    }
    # The adapter already checks model applicability, bounds and provenance.
    # Require matching retained results too; a model's global status cannot
    # stand in for the remaining diseases or an unknown public grade.
    covered = {
        row['code']
        for row in assessment.get('diseaseRisks', [])
        if row.get('code') in audited
        and row.get('status') == 'evaluated'
        and not row.get('reasons')
        and type(row.get('riskIndex')) in {int, float}
        and math.isfinite(row['riskIndex'])
        and 0 <= row['riskIndex'] <= 10
        and row.get('riskLevel') in {'低', '中', '高', 'low', 'medium', 'high', '低风险', '中风险', '高风险'}
    }
    return not pending <= covered


class GutBarrierPipeline:
    version = 'gut-barrier-pipeline-v51.20261010-fixed-catalog-publication'

    def __init__(
        self,
        *,
        sources=None,
        products=None,
        narrative=None,
        risk_models=None,
        risk_model_id=None,
        risk_model_version=None,
        evidence=None,
    ):
        self.sources, self.products, self.narrative = sources, products, narrative
        self.evidence = evidence
        # Selected by server composition only, never by request/model extra fields.
        self.risk_models = risk_models
        self.risk_model_id, self.risk_model_version = risk_model_id, risk_model_version
        if risk_model_id is not None or risk_model_version is not None:
            identity = json.dumps([risk_model_id, risk_model_version], ensure_ascii=False, separators=(',', ':'))
            self.version = type(self).version + ':risk-' + hashlib.sha256(identity.encode()).hexdigest()[:16]

    supports_rule_tools = True

    async def generate(self, request, *, report_id, evaluation_date=None, rule_run_id=None):
        rules = GutBarrierRuleTools(
            {
                name: RuleBinding(RULE_TOOL_VERSIONS[name], handler)
                for name, handler in {
                    'assess': assess_case,
                    'registered_risks': evaluate_registered_risks,
                    'evidence_queries': evidence_queries,
                    'dynamic_assessment': apply_dynamic_assessment,
                    'risk_index': apply_risk_index_rules,
                    'review': apply_review_release,
                    'interventions': build_interventions,
                    'presentation': build_report,
                }.items()
            },
            run_id=rule_run_id or 'gut-rules:' + report_id,
        )
        evaluation_date = evaluation_date or date.today().isoformat()
        source = {'observations': [], 'sources': [], 'issues': [], 'context': {}}
        if self.sources is not None:
            try:
                collected = await self.sources.collect(request, evaluation_date=evaluation_date)
                if not isinstance(collected, dict) or not isinstance(collected.get('context', {}), dict):
                    raise ValueError('invalid source collection')
                for field in ('observations', 'sources', 'issues'):
                    values = collected.get(field)
                    if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
                        raise ValueError('invalid source collection field')
                source = {**collected, 'context': collected.get('context', {})}
            except Exception as error:
                # A failed optional adapter cannot discard verified direct
                # inputs. Cancellation remains owned by the generation lease
                # and global timeout, not converted into a partial success.
                source['issues'].append({'code': 'SOURCE_COLLECTION_UNAVAILABLE', 'errorType': type(error).__name__})
        context = {**source.get('context', {}), 'currentDate': evaluation_date}
        assessment = await rules.invoke(
            'assess',
            request,
            call_id='assessment',
            observations=source.get('observations', []),
            context=context,
            indicator_source='gutHealthIndicator',
        )
        if self.risk_models is not None or self.risk_model_id is not None or self.risk_model_version is not None:
            assessment['diseaseRisks'], assessment['riskModel'] = await rules.invoke(
                'registered_risks',
                assessment,
                call_id='registered-risk',
                registry=self.risk_models,
                model_id=self.risk_model_id,
                version=self.risk_model_version,
                evaluation_date=evaluation_date,
            )
        context = assessment.get('context', context)
        issues = list(source.get('issues', [])) + list(context.get('healthSurveyIssues', []))
        retrieval = {'status': 'not_requested', 'queries': [], 'candidates': [], 'issues': []}
        queries = await rules.invoke('evidence_queries', assessment, call_id='evidence-queries')
        if self.evidence is not None and queries:
            try:
                async with asyncio.timeout(150):
                    retrieval = await self.evidence.retrieve(queries)
                if not isinstance(retrieval, dict) or not isinstance(retrieval.get('candidates'), list):
                    raise ValueError('invalid evidence result')
            except Exception as error:
                retrieval = {
                    'status': 'unavailable',
                    'queries': queries,
                    'candidates': [],
                    'issues': [{'code': 'DISEASE_EVIDENCE_UNAVAILABLE', 'errorType': type(error).__name__}],
                }
            issues.extend(retrieval.get('issues', []))
        assessment = await rules.invoke('dynamic_assessment', assessment, retrieval, call_id='dynamic-assessment')
        assessment = await rules.invoke('risk_index', assessment, call_id='risk-index')
        assessment = await rules.invoke('review', assessment, call_id='review')
        catalog = None
        if self.products is not None and assessment['barrier'].get('score', 0):
            try:
                async with asyncio.timeout(20):
                    catalog = await self.products.fetch()
            except Exception as error:
                issues.append({'code': 'PRODUCT_CATALOG_UNAVAILABLE', 'errorType': type(error).__name__})
        interventions = await rules.invoke(
            'interventions', assessment, call_id='interventions', context=context, product_catalog=catalog
        )
        report = await rules.invoke(
            'presentation',
            assessment,
            interventions,
            call_id='presentation',
            report_id=report_id,
            source_summary=source['sources'],
        )
        narrative_status = 'deterministic'
        # The five-chapter template has a fixed clinical order. Never invoke a
        # global arranger merely to recreate it or replace an entire chapter.
        if self.narrative is not None and report.get('narrativeCandidates'):
            try:
                async with asyncio.timeout(60):
                    report = await self.narrative.arrange(report, request=request, report_id=report_id)
                narrative_status = 'model_arranged'
            except Exception as error:
                issues.append({'code': 'NARRATIVE_FALLBACK', 'errorType': type(error).__name__})
                narrative_status = 'deterministic_fallback'
        report.setdefault('meta', {})['narrativeStatus'] = narrative_status
        report['meta']['pipelineVersion'] = self.version
        # Persisted audit binding only. Public projection intentionally keeps
        # its existing meta keys, without introducing a wire schema change.
        report['meta']['displayTemplateVersion'] = LATEST_DISPLAY_TEMPLATE
        report['meta']['title'] = '肠道屏障功能评估报告'
        report['meta']['evaluationDate'] = context['currentDate']
        # A completed attention rubric does not claim a calibrated prediction
        # model exists. Absence of an optional model is no longer itself a
        # generation failure; a configured model that failed remains partial.
        registered_model_requested = (
            self.risk_models is not None or self.risk_model_id is not None or self.risk_model_version is not None
        )
        partial = bool(
            issues
            or assessment.get('missingData')
            or interventions.get('reviewItems')
            or assessment.get('riskRuleAssessment', {}).get('clinicalCompleteness', {}).get('status') == 'incomplete'
            or assessment.get('barrier', {}).get('status') != 'evaluated'
            or any(item.get('status') != 'evaluated' for item in assessment.get('markers', []))
            or assessment.get('overallStage', {}).get('status') not in {'evaluated', 'display_only'}
            or _incomplete_risk_results(assessment)
            or (registered_model_requested and assessment.get('riskModel', {}).get('status') != 'evaluated')
        )
        # Retain the source-scope statement in the internal report/audit. New
        # current callbacks omit this block; frozen receipts are not rewritten.
        scope = source_scope_block(issues, source['sources'])
        if scope is not None:
            overview = next(section for section in report['sections'] if section.get('id') == 'overview')
            overview['blocks'].insert(0, scope)
        return report, {
            'partial': partial,
            'ruleToolReceipts': rules.audit(),
            'issues': issues,
            'sources': source['sources'],
            'ruleVersion': assessment.get('ruleVersion'),
            'contentVersion': CONTENT_VERSION,
            'templateVersion': TEMPLATE_VERSION,
            'displayTemplateVersion': LATEST_DISPLAY_TEMPLATE,
            'reviewVersion': REVIEW_VERSION,
            'referenceSelection': reference_selection_audit(),
            'reportReview': assessment.get('reportReview'),
            'overallStage': assessment.get('overallStage'),
            'riskModel': assessment.get('riskModel'),
            'riskRuleAssessment': assessment.get('riskRuleAssessment'),
            'diseaseAssessment': assessment.get('diseaseAssessment'),
            'missingData': assessment.get('missingData', []),
            'reviewItems': interventions.get('reviewItems', []),
        }
