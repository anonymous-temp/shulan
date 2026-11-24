"""Run the production evaluate pipeline locally, without Mongo, Redis or callbacks.

The request JSON has the same shape as ``POST /api/v1/report``. Remote files
may be mapped to local paths so that originals never have to be uploaded:

    python scripts/generate_evaluation_report.py \
        --input request.json \
        --file https://local.invalid/x/report.pdf=/path/report.pdf \
        --output _local_runs/case-01

Model, evidence and tool credentials come from the process environment
(``SHULAN_*``); this script never reads a private ``.env`` by itself. It
writes ``callback.json`` (the public evaluate callback), ``audit.json`` (the
private generation audit) and ``report.html`` (a local preview). The HTML is a
preview only and is never an API artifact.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_INERT_DEFAULTS = {
    'SHULAN_ENVIRONMENT': 'dev',
    'SHULAN_LOG_FILE_ENABLED': 'false',
    'SHULAN_ALIYUN_DOCMIND_ACCESSKEY_ID': 'unused-local',
    'SHULAN_ALIYUN_DOCMIND_ACCESSKEY_SECRET': 'unused-local',
    'SHULAN_ALIYUN_DOCMIND_ENDPOINT': 'docmind-api.cn-hangzhou.aliyuncs.com',
    'SHULAN_MONGODB_URI': 'mongodb://127.0.0.1:1/unused',
    'SHULAN_MONGODB_DATABASE': 'unused_local',
    'SHULAN_MONGODB_COLL_SESSIONS': 'unused_sessions',
    'SHULAN_MONGODB_COLL_HEALTH_REPORTS': 'unused_reports',
    'SHULAN_REPORT_CALLBACK_URL': 'http://127.0.0.1:9/unused-local-callback',
    'SHULAN_REPORT_PRODUCT_URL': 'http://127.0.0.1:9/unused-local-products',
}


def _arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--input', required=True, help='request JSON in the /api/v1/report shape')
    parser.add_argument('--output', required=True, help='new output directory')
    parser.add_argument(
        '--file',
        action='append',
        default=[],
        metavar='URL=PATH',
        help='serve a request URL from a local file (repeatable)',
    )
    parser.add_argument('--report-id', default=None, help='local finalReportId; defaults to a timestamp')
    parser.add_argument('--no-html', action='store_true', help='skip the local HTML preview')
    return parser.parse_args(argv)


def _local_files(pairs):
    mapping = {}
    for pair in pairs:
        url, separator, path = pair.partition('=')
        if not separator or not url.strip() or not path.strip():
            raise SystemExit('--file expects URL=PATH')
        target = Path(path).expanduser().resolve()
        if not target.is_file():
            raise SystemExit(f'local file not found: {target}')
        mapping[url.strip()] = target
    return mapping


def _build_pipeline(local_files):
    """Compose the production evaluate pipeline with in-memory collaborators."""
    from adapters.report.agent_narrative import NarrativeClientFactory
    from adapters.report.callback import CallbackClient
    from adapters.report.clients import EviMedClient
    from adapters.report.clients import SourceClient
    from adapters.report.clients import _detect_type
    from adapters.report.public_evidence import PublicEvidenceClient
    from agents import AgentHub
    from agents import AgentRuntime
    from agents import GraphRegistry
    from agents.graphs.report import register_report_narrative
    from agents.llm.aion_gateway import AionLLMGateway
    from agents.llm.profiles import ModelProfileRegistry
    from config import env
    from modules.report.fallback_policy import DEFAULT_REPORT_FALLBACK_POLICY
    from modules.report.pipeline import ReportPipeline
    from modules.report.ports import DownloadedFile

    class LocalSourceClient(SourceClient):
        """Serve explicitly mapped originals from disk; everything else is unchanged."""

        async def fetch_file(self, source):
            path = local_files.get(source.url)
            if path is None:
                return await super().fetch_file(source)
            content = await asyncio.to_thread(path.read_bytes)
            detected = _detect_type(content)
            if source.file_type != detected:
                raise ValueError(f'{path.name}: fileType={source.file_type} does not match content')
            return DownloadedFile(
                filename=path.name,
                content_type='application/pdf' if detected == 'pdf' else 'image/*',
                content=content,
                declared_type=source.file_type,
            )

    registry = GraphRegistry()
    runtime = AgentRuntime(
        hub=AgentHub(registry),
        registry=registry,
        profiles=ModelProfileRegistry(),
        llm_gateway=AionLLMGateway(
            provider=env.LLM_PROVIDER or 'deepseek',
            api_key=env.LLM_API_KEY,
            default_model=env.LLM_DEFAULT_MODEL or None,
        ),
        context_repository=None,
        lease_manager=None,
        checkpointer=None,
        _close_callback=lambda: None,
    )
    register_report_narrative(runtime)
    policy = DEFAULT_REPORT_FALLBACK_POLICY

    async def no_session(user_id, session_id):
        return None

    extras = {}
    try:  # Optional collaborators added on this branch; absent on older revisions.
        from adapters.report.factory import build_local_evidence_extras

        extras = build_local_evidence_extras()
    except ImportError:
        pass

    pipeline = ReportPipeline(
        clinical_report_enabled=True,
        source_client=LocalSourceClient(policy=policy),
        evidence_client=EviMedClient(
            base_url=env.EVIMED_API_URL,
            api_key=env.EVIMED_API_KEY,
            timeout_seconds=env.EVIMED_TIMEOUT,
            attempts=policy.evidence_attempts,
        ),
        public_evidence_client=PublicEvidenceClient(
            ncbi_api_key=env.NCBI_API_KEY, contact_email=env.NCBI_CONTACT_EMAIL, crossref_mailto=env.CROSSREF_MAILTO
        ),
        callback_client=CallbackClient(url=env.REPORT_CALLBACK_URL, policy=policy),
        session_loader=no_session,
        narrative_client=None,
        narrative_client_factory=NarrativeClientFactory(hub=runtime.hub, policy=policy),
        artifact_store=None,
        generation_timeout_seconds=env.REPORT_GENERATION_TIMEOUT_SECONDS,
        **extras,
    )
    return pipeline, runtime


async def _run(args):
    from modules.report.artifacts import bind_report_batch
    from modules.report.artifacts import reset_report_batch
    from modules.report.evaluation_callback import build_evaluation_callback
    from modules.report.schemas import ReportRequest

    output = Path(args.output).expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        raise SystemExit('output directory must be new or empty')
    output.mkdir(parents=True, exist_ok=True)
    request = ReportRequest.model_validate_json(Path(args.input).read_text(encoding='utf-8'))
    pipeline, runtime = _build_pipeline(_local_files(args.file))
    report_id = args.report_id or 'local-' + time.strftime('%Y%m%d-%H%M%S')
    token = bind_report_batch(
        store=None, final_report_id=report_id, user_id=request.user_id, session_id=request.session_id
    )
    started = time.monotonic()
    try:
        payload = await pipeline._generate_evaluate_payload(request)
        body = build_evaluation_callback(payload)
    finally:
        reset_report_batch(token)
        await pipeline.aclose()
        runtime.close()
    elapsed = round(time.monotonic() - started, 1)
    (output / 'callback.json').write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding='utf-8')
    document = getattr(getattr(payload, 'report', None), 'clinical_document', None)
    generation = (getattr(document, 'background', None) or {}).get('reportGeneration', {})
    audit = getattr(payload, 'audit', None)
    private = {
        'finalReportId': report_id,
        'elapsedSeconds': elapsed,
        'status': body.get('status'),
        'partialReasons': body.get('partialReasons'),
        'generation': generation,
        'audit': audit.model_dump(mode='json', by_alias=True) if audit is not None else None,
    }
    (output / 'audit.json').write_text(json.dumps(private, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
    if not args.no_html:
        try:
            from scripts.render_evaluation_report import render_evaluation_html

            (output / 'report.html').write_text(render_evaluation_html(body), encoding='utf-8')
        except Exception as error:  # the preview never decides the run's outcome
            print(f'HTML preview skipped: {type(error).__name__}: {error}', file=sys.stderr)
    print(
        json.dumps(
            {
                'output': str(output),
                'status': body.get('status'),
                'partialReasons': body.get('partialReasons'),
                'elapsedSeconds': elapsed,
            },
            ensure_ascii=False,
        )
    )


def main(argv=None):
    args = _arguments(argv)
    for key, value in _INERT_DEFAULTS.items():
        os.environ.setdefault(key, value)
    if not os.environ.get('SHULAN_LLM_API_KEY', '').strip():
        raise SystemExit('SHULAN_LLM_API_KEY is required in the process environment')
    asyncio.run(_run(args))


if __name__ == '__main__':
    main()
