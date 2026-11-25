"""Re-run the model-driven case authoring from a local checkpoint.

``scripts/generate_evaluation_report.py --checkpoint`` pickles the inputs of
the six-chapter projection. This script rebuilds the fact sheet from that
checkpoint, runs the whole-case analysis, the section writers and the review
with the current guides, then projects the result into the callback:

    python scripts/run_assessment_case.py RUN_DIR [--output DIR] [--stage analysis|write|all] [--reuse]

It writes ``sheet.txt`` (what the model reads), ``case.json`` (analysis plan,
written cells and audit), ``callback_v2.json`` (the ``assessment.v2`` body)
and ``report.html``. ``--reuse`` keeps an existing ``case.json`` and only
repeats the projection, so presentation changes need no model call. Model
credentials come from the process environment (``SHULAN_*``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pickle
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('run_dir', help='output directory of a --checkpoint run')
    parser.add_argument('--output', default=None, help='where to write (default RUN_DIR/case)')
    parser.add_argument(
        '--stage',
        choices=('analysis', 'write', 'all'),
        default='all',
        help='stop after the analysis, after writing, or run the review as well',
    )
    parser.add_argument('--reuse', action='store_true', help='reuse OUTPUT/case.json and only repeat the projection')
    parser.add_argument('--timeout', type=float, default=1200.0, help='time budget of the model steps in seconds')
    return parser.parse_args(argv)


def load_case_inputs(run_dir: Path):
    """Checkpointed projection inputs plus the fact sheet built from them."""
    from modules.report.assessment_case_report import case_inputs

    kwargs = pickle.loads((run_dir / 'presentation_inputs.pkl').read_bytes())
    return kwargs, case_inputs(
        request=kwargs['request'],
        parsed=kwargs['parsed'],
        document=kwargs['document'],
        data=kwargs['data'],
        blueprint=kwargs['blueprint'],
    )


async def _author(args, kwargs, inputs, output: Path):
    from modules.report.assessment_case_authoring import _generate
    from modules.report.assessment_case_authoring import analysis_payload
    from modules.report.assessment_case_authoring import author_assessment_case
    from modules.report.assessment_case_authoring import write_case_sections
    from modules.report.assessment_case_plan import apply_priority_safety_net
    from modules.report.assessment_case_plan import normalize_case_analysis
    from modules.report.assessment_case_plan import plan_statistics
    from scripts.generate_evaluation_report import _build_pipeline

    pipeline, runtime = _build_pipeline({})
    request = kwargs['request']
    client = pipeline.narrative_client_factory.create(
        user_id=request.user_id,
        session_id=request.session_id,
        execution_id='local-case-' + time.strftime('%H%M%S'),
    )
    audit: dict = {}
    deadline = time.monotonic() + args.timeout
    sheet = inputs['sheet']
    try:
        if args.stage == 'all':
            case = await author_assessment_case(
                client,
                sheet,
                clinical=inputs['clinical'],
                microbiome=inputs['microbiome'],
                flags=inputs['flags'],
                deadline=deadline,
                audit=audit,
            )
        else:
            raw = await _generate(client, analysis_payload(sheet), deadline, audit, 'analysis')
            (output / 'analysis_raw.json').write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding='utf-8')
            plan = normalize_case_analysis(raw, sheet, clinical=inputs['clinical'])
            apply_priority_safety_net(plan, sheet, inputs['flags'])
            audit['plan'] = plan_statistics(plan)
            cells, limits = {}, {}
            if args.stage == 'write':
                cells, limits = await write_case_sections(
                    client,
                    plan,
                    sheet,
                    clinical=inputs['clinical'],
                    microbiome=inputs['microbiome'],
                    deadline=deadline,
                    audit=audit,
                )
            case = {'plan': plan, 'cells': cells, 'limits': limits, 'raw': raw}
    finally:
        await pipeline.aclose()
        runtime.close()
    case['audit'] = audit
    return case


def write_case_outputs(run_dir: Path, output: Path, kwargs, inputs, case) -> dict:
    """Project the authored case into the callback and the local preview."""
    try:
        from modules.report.assessment_case_projection import build_case_presentation
    except ImportError:
        return {}
    from modules.report.evaluation_callback import build_frontend_evaluation_callback
    from scripts.render_evaluation_report import render_evaluation_html

    # The checkpoint still carries the bibliography of its original run.
    library = getattr(getattr(kwargs.get('data'), 'appendices', None), 'reference_literature', None)
    if library is not None:
        library.clear()
    view = build_case_presentation(
        request=kwargs['request'],
        parsed=kwargs['parsed'],
        document=kwargs['document'],
        data=kwargs['data'],
        clinical=inputs['clinical'],
        microbiome=inputs['microbiome'],
        sheet=inputs['sheet'],
        case=case,
        timing=inputs['timing'],
    )
    body = json.loads((run_dir / 'callback.json').read_text(encoding='utf-8'))
    body['data']['assessmentPresentation'] = view.model_dump(mode='json', by_alias=True)
    library = getattr(getattr(kwargs.get('data'), 'appendices', None), 'reference_literature', None)
    if library is not None:
        body['data'].setdefault('appendices', {})['referenceLiterature'] = [
            entry.model_dump(mode='json', by_alias=True) for entry in library
        ]
    (output / 'callback.json').write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding='utf-8')
    frontend = build_frontend_evaluation_callback(body)
    (output / 'callback_v2.json').write_text(json.dumps(frontend, ensure_ascii=False, indent=1), encoding='utf-8')
    (output / 'report.html').write_text(render_evaluation_html(body), encoding='utf-8')
    return {'callback': str(output / 'callback_v2.json')}


def main(argv=None):
    from scripts.generate_evaluation_report import _INERT_DEFAULTS

    args = _arguments(argv)
    for key, value in _INERT_DEFAULTS.items():
        os.environ.setdefault(key, value)
    if args.reuse:
        os.environ.setdefault('SHULAN_LLM_API_KEY', 'unused-local')
    elif not os.environ.get('SHULAN_LLM_API_KEY', '').strip():
        raise SystemExit('SHULAN_LLM_API_KEY is required in the process environment')
    run_dir = Path(args.run_dir).expanduser().resolve()
    output = Path(args.output).expanduser().resolve() if args.output else run_dir / 'case'
    output.mkdir(parents=True, exist_ok=True)
    kwargs, inputs = load_case_inputs(run_dir)
    (output / 'sheet.txt').write_text(inputs['sheet'].text, encoding='utf-8')
    case_path = output / 'case.json'
    started = time.monotonic()
    if args.reuse and case_path.is_file():
        case = json.loads(case_path.read_text(encoding='utf-8'))
    elif args.reuse and kwargs.get('case') is not None:
        # The checkpoint of a whole-case run already holds its reviewed plan and cells.
        case = {key: kwargs['case'].get(key) for key in ('plan', 'cells', 'limits', 'raw', 'audit')}
    else:
        case = asyncio.run(_author(args, kwargs, inputs, output))
        case_path.write_text(json.dumps(case, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
    summary = {
        'output': str(output),
        'seconds': round(time.monotonic() - started, 1),
        'plan': case.get('audit', {}).get('plan'),
        'cells': len(case.get('cells') or {}),
    }
    if case.get('cells'):
        summary.update(write_case_outputs(run_dir, output, kwargs, inputs, case))
    print(json.dumps(summary, ensure_ascii=False, default=str))


if __name__ == '__main__':
    main()
