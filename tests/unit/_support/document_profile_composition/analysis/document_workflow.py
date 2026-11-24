"""Shared synthetic fixture closure extracted from tests.unit.services.analysis.test_document_workflow."""

from unittest.mock import AsyncMock

from adapters.analysis.document_index import MongoDocumentIndexStore
from health_profile.document_observations import MongoDocumentObservationStore
from modules.analysis.application.output_profiles import DocumentOutputCoordinator
from tests.unit._support.document_profile_composition.analysis.document_reuse import reuse_flow
from tests.unit._support.document_profile_composition.analysis.fakes import MemoryMongoCollection


def extended_flow(extractor=None):
    flow = reuse_flow()
    flow.reports._revisions = MemoryMongoCollection()
    flow.observations = MongoDocumentObservationStore(MemoryMongoCollection())
    flow.source._observation_store = flow.observations
    flow.output_index = MongoDocumentIndexStore(MemoryMongoCollection(), identity_key='extractionKey')
    flow.extractor = extractor or AsyncMock(
        return_value={
            'sample': {'reportDate': '2026-09-01'},
            'metrics': [{'name': '菌群多样性', 'value': '68', 'referenceRange': '70-100', 'status': '偏低'}],
            'taxa': [{'latinName': 'Faecalibacterium prausnitzii', 'value': '0.0296', 'unit': '%'}],
            'supplementaryHospitalData': [
                {
                    'dataType': 'INSPECTION',
                    'itemName': '低密度脂蛋白胆固醇',
                    'testResultValue': '3.48 mmol/L',
                    'reportTime': '2024-11-02',
                }
            ],
            'sourceChecksum': 'untrusted-extractor-value',
        }
    )
    flow.service.output_profiles.register_report_source(flow.extractor, version='test-report-source-v1')
    flow.service._document_outputs = DocumentOutputCoordinator(
        flow.service, flow.output_index, flow.service.output_profiles
    )
    flow.service._document_outputs.POLL_SECONDS = 0.005
    return flow
