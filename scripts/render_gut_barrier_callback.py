"""Render a local HTML preview from an already generated callback JSON.

No HTTP requests, model calls, reassessment or callback delivery occur here.
The source report is copied unchanged; HTML is not a production API artifact.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.gut_barrier.rendering import render_report_html
from adapters.gut_barrier.render_context import make_render_context, resolve_render_context, render_context_from_headers
from modules.gut_barrier.display_versions import resolve_display_template, SUPPORTED_DISPLAY_TEMPLATES
from modules.gut_barrier.publication import SUPPORTED_CALLBACK_TYPES
from scripts.artifact_paths import artifact_output


def render_callback(callback: dict, output: Path, *, template_version=None, render_context=None,
                    callback_headers=None, template_archive_dir: str | Path | None = None) -> Path:
    output = artifact_output(output)
    if not isinstance(callback, dict) or callback.get("type") not in SUPPORTED_CALLBACK_TYPES:
        raise ValueError("expected the final gutBarrier callback object")
    if callback.get("status") not in {"success", "partial"}:
        raise ValueError("only a generated success/partial report can be previewed")
    report = callback.get("report")
    if not isinstance(report, dict) or not report.get("sections"):
        raise ValueError("callback has no renderable report")
    if callback_headers is not None:
        header_context = render_context_from_headers(callback, callback_headers)
        if header_context is not None:
            if render_context is not None and header_context != render_context:
                raise ValueError("callback headers and render context disagree")
            render_context = header_context
    # Compute before writing so an invalid report cannot leave a false preview.
    selected = resolve_display_template(report, template_version)
    if render_context is not None:
        bound = resolve_render_context(report, render_context)
        if template_version is not None and bound != selected:
            raise ValueError("explicit template and render context disagree")
        selected = bound
    archive_options = {"template_archive_dir": template_archive_dir} if template_archive_dir is not None else {}
    html = render_report_html(report, template_version=selected, **archive_options)
    serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any((output / name).exists() for name in ("report.json", "report.html", "render-context.json")):
        raise FileExistsError("refusing to overwrite an existing report preview")
    (output / "report.json").write_text(serialized, encoding="utf-8")
    target = output / "report.html"
    target.write_text(html, encoding="utf-8")
    (output / "render-context.json").write_text(json.dumps(make_render_context(report, selected), ensure_ascii=False, indent=2), encoding="utf-8")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--callback", required=True, help="Saved final callback JSON, not the admission response")
    parser.add_argument("--output", help="New directory outside the checkout; defaults to ~/Documents/ShulanArtifacts")
    parser.add_argument("--template-version", choices=sorted(SUPPORTED_DISPLAY_TEMPLATES),
                        help="Explicit frontend release; bare historical callbacks default to static.v1")
    parser.add_argument("--template-archive-dir", help="Absolute external archive directory for historical v1-v5 templates; "
                        "must be supplied explicitly for historical previews. Current v6 is bundled.")
    parser.add_argument("--render-context", help="Trusted matching render-context.json sidecar; never inferred from date/title")
    parser.add_argument("--callback-headers", help="Saved authenticated callback HTTP headers as a JSON object")
    args = parser.parse_args()
    callback = json.loads(Path(args.callback).read_text(encoding="utf-8"))
    context = json.loads(Path(args.render_context).read_text(encoding="utf-8")) if args.render_context else None
    headers = json.loads(Path(args.callback_headers).read_text(encoding="utf-8")) if args.callback_headers else None
    target = render_callback(callback, artifact_output(args.output, prefix="gut-barrier-preview"), template_version=args.template_version,
                             render_context=context, callback_headers=headers, template_archive_dir=args.template_archive_dir)
    print(json.dumps({"preview": str(target), "source": "callback_json", "reassessed": False}, ensure_ascii=False))
