"""Lazy application facade for the existing health-profile domain package.

Keeping this facade lazy prevents a web-only import from constructing or even
loading MongoDB adapters. Public names remain stable while the underlying
package is migrated incrementally.
"""

from __future__ import annotations

from importlib import import_module

_ROOT_EXPORTS = {
    'ChatProfileDeliveryResult',
    'ChatProfileSourceService',
    'HealthProfileChatMemoryProvider',
    'HealthProfileConflictError',
    'HealthProfileIngestionWorker',
    'HealthProfileJobMaintenanceService',
    'HealthProfileJobNotFoundError',
    'HealthProfileJobRedriveConflictError',
    'HealthProfileNotFoundError',
    'HealthProfileService',
    'HealthProfileValidationError',
    'HealthReportSaveResult',
    'HealthReportSourceConflictError',
    'HealthReportSourceService',
    'ProfileBootstrapResult',
    'RecordProfileDeliveryResult',
    'RecordProfileSourceService',
    'RepositoryHealthProfileChatMemoryProvider',
    'SubjectUpsertResult',
}
_FACTORY_EXPORTS = {
    'create_application_chat_profile_source',
    'create_application_health_profile_chat_memory_provider',
    'create_application_health_profile_ingestion_worker',
    'create_application_health_profile_job_maintenance_service',
    'create_application_health_profile_service',
    'create_application_health_report_source',
    'create_application_record_profile_source',
}
_PUBLIC_EXPORTS = {
    'ProfileBasicInfoService': 'health_profile.basic_info',
    'MongoBasicInfoStore': 'health_profile.basic_info_store',
    'BasicInfoConflict': 'health_profile.basic_info_store',
    'BasicInfoUnavailable': 'health_profile.basic_info_store',
    'create_profile_evidence_policy': 'modules.health_profile.evidence_policy',
    'ProfileEvidencePolicyUnsupportedError': 'modules.health_profile.evidence_policy',
    'MongoProfileSourcePresenceReader': 'health_profile.source_presence',
    'ProfileFreshSourceAdmission': 'health_profile.source_admission',
    'ProfileSourceMaterialArchive': 'health_profile.source_archive',
    'ProfileDocumentSourceResolver': 'health_profile.document_source',
    'ProfileCurrentDocumentSourceResolver': 'health_profile.document_source_current',
    'ProfileLegacyIngestionStatusReader': 'health_profile.legacy_ingestion_status',
    'ProfileUserSourceResolver': 'health_profile.source_extraction',
    'DurableProfileSourceExtractor': 'health_profile.source_extraction',
    'ProfileSnapshotReader': 'health_profile.snapshots',
    'ProfileSourceInbox': 'health_profile.source_publications',
    'MongoProfilePublicationStore': 'health_profile.source_publications',
    'ProfileSourceConflictError': 'health_profile.source_publications',
    'ProfileSourceApplier': 'health_profile.source_application',
    'ProfilePublicationWorker': 'health_profile.source_application',
    'ProfileSourceMaterial': 'health_profile.source_material',
}
_FACTORY_EXPORTS.add('create_application_profile_public_contracts')
_FACTORY_EXPORTS.add('create_application_profile_source_admission')
__all__ = sorted(_ROOT_EXPORTS | _FACTORY_EXPORTS | _PUBLIC_EXPORTS.keys())


def __getattr__(name: str):
    if name in _PUBLIC_EXPORTS:
        return getattr(import_module(_PUBLIC_EXPORTS[name]), name)
    if name in _ROOT_EXPORTS:
        return getattr(import_module('health_profile'), name)
    if name in _FACTORY_EXPORTS:
        return getattr(import_module('health_profile.application'), name)
    raise AttributeError(name)
