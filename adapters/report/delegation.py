"""Production adapters for Report's public durable peer follow-up service."""

from __future__ import annotations

from adapters.report.agent_followup import FollowupModelGatewayFactory
from adapters.report.followup import AionFollowupReportGateway
from modules.report.fallback_policy import DEFAULT_REPORT_FALLBACK_POLICY


class LegacyGutBarrierFollowupGenerator:
    def __init__(self, *, hub, gateway):
        self.models = FollowupModelGatewayFactory(hub=hub)
        self.gateway = gateway

    async def generate(self, body, *, user_id, session_id, execution_id):
        # Private implementation is contained within the Report module boundary.
        from modules.report.reports import _push_report

        return await _push_report(
            body,
            user_id,
            session_id,
            policy=DEFAULT_REPORT_FALLBACK_POLICY,
            gateway=self.gateway,
            model_gateway=self.models.create(user_id=user_id, session_id=session_id, execution_id=execution_id),
        )


def build_report_followup_service(
    *,
    hub,
    database,
    callback_url,
    product_catalog_url,
    heartbeat_seconds=30,
    concurrency=2,
    generation_timeout_seconds=1800,
):
    from pymongo.write_concern import WriteConcern

    from adapters.report.delegation_store import MongoReportFollowupStore
    from modules.report.delegation import ReportFollowupService

    concern = WriteConcern(w='majority', j=True, wtimeout=5000)
    store = MongoReportFollowupStore(
        batches=database['report_followup_jobs'].with_options(write_concern=concern),
        artifacts=database['report_followup_artifacts'].with_options(write_concern=concern),
        deliveries=database['report_followup_deliveries'].with_options(write_concern=concern),
    )
    generator = LegacyGutBarrierFollowupGenerator(
        hub=hub,
        gateway=AionFollowupReportGateway(callback_url=callback_url, product_catalog_url=product_catalog_url),
    )
    return ReportFollowupService(
        store=store,
        generator=generator,
        callback_url=callback_url,
        heartbeat_seconds=heartbeat_seconds,
        concurrency=concurrency,
        generation_timeout_seconds=generation_timeout_seconds,
    )
