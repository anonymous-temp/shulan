"""Preview the production request/pipeline locally; HTML is never an API artifact."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.gut_barrier.rendering import render_report_html
from adapters.gut_barrier.render_context import make_render_context, render_context_headers
from adapters.gut_barrier.sources import GutBarrierSources
from modules.gut_barrier.pipeline import GutBarrierPipeline
from modules.gut_barrier.publication import CALLBACK_SCHEMA_VERSION, CALLBACK_TYPE, project_callback_report
from modules.gut_barrier.schemas import GutBarrierRequest
from scripts.artifact_paths import artifact_output


class OfflineSources:
    async def collect(self, request, *, evaluation_date=None):
        has_sources = bool(request.file_list or request.test_data)
        return {"observations": [], "sources": [], "context": {},
                "issues": [{"code": "SOURCES_SKIPPED_OFFLINE"}] if has_sources else []}


async def generate(args):
    directory = artifact_output(getattr(args, "output", None))
    # A preview must not overwrite an earlier receipt or frozen report.
    if directory.exists():
        raise FileExistsError("preview output directory must be new")
    request = GutBarrierRequest.model_validate_json(Path(args.input).read_text(encoding="utf-8"))
    parsing_client = None
    if getattr(args, "ocr", False):
        if args.offline:
            raise ValueError("--ocr requires online source processing; cannot combine with --offline")
        from adapters.analysis import create_application_document_parsing_client
        parsing_client = create_application_document_parsing_client()
    pipeline = GutBarrierPipeline(sources=OfflineSources() if args.offline else GutBarrierSources(parsing_client=parsing_client))
    # A new algorithm release must not impersonate an earlier local report ID.
    # Production IDs remain exclusively controlled by the durable service.
    digest = hashlib.sha256(json.dumps(
        [pipeline.version, request.model_dump(mode="json", by_alias=True), args.evaluation_date],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()
    evidence_client = evidence = None
    if getattr(args, "evidence", False) and not args.offline:
        from config.env import EVIMED_API_URL, EVIMED_API_KEY
        from adapters.report.clients import EviMedClient
        from adapters.gut_barrier.evidence import GutBarrierEvidence
        evidence_client = EviMedClient(base_url=EVIMED_API_URL, api_key=EVIMED_API_KEY,
                                      timeout_seconds=10, attempts=1, result_count=4)
        evidence = GutBarrierEvidence(evimed_client=evidence_client, max_queries=12,
                                      max_results=36, per_query_results=3, timeout_seconds=120)
        pipeline.evidence = evidence
    try:
        report, audit = await pipeline.generate(request, report_id="gbr_" + digest,
                                               evaluation_date=args.evaluation_date)
    finally:
        if evidence is not None:
            await evidence.aclose()
        if evidence_client is not None:
            await evidence_client.aclose()
    report.setdefault("meta", {})["previewOnly"] = True
    public_report = project_callback_report(report, reference_audit=audit)
    callback = {"type": CALLBACK_TYPE, "schemaVersion": CALLBACK_SCHEMA_VERSION,
                "finalReportId": report["meta"]["reportId"], "userId": request.user_id,
                "sessionId": request.session_id, "status": "partial" if audit["partial"] else "success",
                "report": public_report, "error": None}
    audit = {**audit, "reportDocument": report, "previewOnly": True, "callbackSent": False}
    render_context = make_render_context(public_report, audit["displayTemplateVersion"])
    rendered = render_report_html(public_report, template_version=render_context["templateVersion"])
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    for filename, payload in (("report.json", public_report), ("callback.json", callback), ("audit.json", audit)):
        (directory / filename).write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    (directory / "render-context.json").write_text(json.dumps(render_context, ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "callback-headers.json").write_text(json.dumps(
        render_context_headers(callback, render_context), ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "report.html").write_text(rendered, encoding="utf-8")
    print(json.dumps({"report": str(directory / "report.html"), "partial": audit["partial"],
                      "source": "local_pipeline", "callbackSent": False}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", help="New directory outside the checkout; defaults to ~/Documents/ShulanArtifacts")
    parser.add_argument("--offline", action="store_true", help="Explicitly skip remote sources, no model or product calls")
    parser.add_argument("--ocr", action="store_true", help="Use the configured production DocMind client for image/scanned pages")
    parser.add_argument("--evidence", action="store_true", help="Retrieve de-identified EviMed / Europe PMC candidate evidence as in the API")
    parser.add_argument("--evaluation-date", type=lambda value: date.fromisoformat(value).isoformat(),
                        help="Freeze a local preview evaluation day (YYYY-MM-DD); API uses admission day")
    asyncio.run(generate(parser.parse_args()))
