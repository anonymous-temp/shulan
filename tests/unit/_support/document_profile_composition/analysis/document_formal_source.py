"""Shared synthetic fixture closure extracted from tests.unit.services.analysis.test_document_formal_source."""

from datetime import timedelta

from adapters.analysis.formal_source import create_document_formal_source_service
from health_profile.source_archive import ProfileSourceMaterialArchive
from modules.analysis.formal_source_contracts import FORMAL_SOURCE_REVISION
from modules.analysis.formal_source_contracts import DocumentFormalSourceCommand
from tests.unit._support.document_profile_composition.analysis.document_formal_source_store import Collection
from tests.unit._support.document_profile_composition.analysis.document_fresh_profile_composition import composition
from tests.unit._support.document_profile_composition.analysis.document_fresh_profile_composition import options
from tests.unit._support.document_profile_composition.analysis.document_managed_source import restart


async def flow():
    current = await composition()
    current.command_records = options(Collection())
    current.archive = ProfileSourceMaterialArchive(
        report_store=current.env.flow.reports,
        publication_store=current.profile.inbox.store,
        clock=lambda: current.profile.now[0],
    )
    current.runtime = restart(current)
    current.formal_records = Collection()
    current.formal_records.with_options = lambda **kwargs: current.formal_records
    current.formal = build(current)
    return current


def build(current, **kwargs):
    return create_document_formal_source_service(
        compatibility=current.env.api,
        source_admission=current.admission,
        managed=current.runtime,
        report_publications=current.env.publisher,
        profile_admission=current.fresh,
        command_records=current.formal_records,
        clock=lambda: current.profile.now[0].timestamp(),
        **kwargs,
    )


def request(current, *, identity='business', operation='base', inputs=None):
    return DocumentFormalSourceCommand(
        current.scope,
        identity,
        operation,
        inputs or (current.item,),
        FORMAL_SOURCE_REVISION,
    )


async def advance(current, command, count=6):
    for _ in range(count):
        await current.formal.recover_pending()
        receipt = await current.formal.read_receipt(scope=current.scope, command_id=command.command_id)
        if receipt['status'] != 'pending':
            return receipt
        current.profile.now[0] += timedelta(seconds=31)
    return receipt


async def apply(current):
    await current.profile.worker.recover_pending()
    current.profile.now[0] += timedelta(minutes=1)
    await current.recovery.recover_pending()
