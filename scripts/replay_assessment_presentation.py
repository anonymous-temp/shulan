"""Re-run the six-chapter projection offline from a local checkpoint.

``scripts/generate_evaluation_report.py --checkpoint`` pickles the projection
inputs to ``presentation_inputs.pkl``. This script re-runs only the
deterministic part (projection, callback assembly and the local preview) with
the current code, so presentation and rendering changes can be checked in
seconds without any model call:

    python scripts/replay_assessment_presentation.py RUN_DIR [--output DIR]

It writes ``callback.json`` (local shape), ``callback_v2.json`` (the
``assessment.v2`` body the frontend receives), ``report.html`` and
``issues.json`` (projection gaps) to the output directory (default
``RUN_DIR/replay``). Model-written cells are reused as they were accepted in
the original run; changing prompts or plans still needs a full run.
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('run_dir', help='output directory of a --checkpoint run')
    parser.add_argument('--output', default=None, help='where to write the replay (default RUN_DIR/replay)')
    return parser.parse_args(argv)


def replay(run_dir: Path, output: Path) -> dict:
    from modules.report.assessment_presentation import assessment_projection_issues
    from modules.report.assessment_presentation import build_assessment_presentation
    from modules.report.evaluation_callback import build_frontend_evaluation_callback
    from scripts.render_evaluation_report import render_evaluation_html

    kwargs = pickle.loads((run_dir / 'presentation_inputs.pkl').read_bytes())
    view = build_assessment_presentation(**kwargs)
    issues = assessment_projection_issues(view, kwargs['blueprint'])
    body = json.loads((run_dir / 'callback.json').read_text(encoding='utf-8'))
    body['data']['assessmentPresentation'] = (
        view.model_dump(mode='json', by_alias=True) if hasattr(view, 'model_dump') else view
    )
    output.mkdir(parents=True, exist_ok=True)
    (output / 'callback.json').write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding='utf-8')
    frontend = build_frontend_evaluation_callback(body)
    (output / 'callback_v2.json').write_text(json.dumps(frontend, ensure_ascii=False, indent=1), encoding='utf-8')
    (output / 'report.html').write_text(render_evaluation_html(body), encoding='utf-8')
    (output / 'issues.json').write_text(json.dumps(issues, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
    return {'output': str(output), 'issues': len(issues) if hasattr(issues, '__len__') else issues}


def main(argv=None):
    import os

    from scripts.generate_evaluation_report import _INERT_DEFAULTS

    args = _arguments(argv)
    # Replay never calls a model or a remote service; the settings only have
    # to pass configuration validation.
    for key, value in {**_INERT_DEFAULTS, 'SHULAN_LLM_API_KEY': 'unused-local'}.items():
        os.environ.setdefault(key, value)
    run_dir = Path(args.run_dir).expanduser().resolve()
    output = Path(args.output).expanduser().resolve() if args.output else run_dir / 'replay'
    print(json.dumps(replay(run_dir, output), ensure_ascii=False))


if __name__ == '__main__':
    main()
