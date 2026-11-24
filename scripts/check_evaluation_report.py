"""Reader-facing quality check of an evaluate callback.

    python scripts/check_evaluation_report.py callback.json [more.json ...]

Prints, per report, what must never reach the reader (other parties such as
the testing vendor or the original report, capture markers such as
"self-reported, unverified", English codes, addressing the reader as "您"),
the machine-writing patterns left in the prose, how much of the prose is
itemised, long paragraphs, lead-ins that narrate the input a fact came from,
and empty required sections. Exit status 1 when anything that must never
reach the reader is found.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _sections(data):
    overview = data.health_overview
    return {
        'overallSummary': bool(overview.overall_summary.text),
        'keyFindings': bool(overview.key_findings),
        'scores': sum(row.score is not None for row in overview.assessment_chart),
        'findingsExplained': sum(bool(row.explanation) for row in data.medical_assessment.findings),
        'findings': len(data.medical_assessment.findings),
        'nextSteps': sum(bool(row.description) for row in data.comprehensive_conclusion.next_steps),
        'firstMeasures': sum(bool(row.followup_action) for row in data.comprehensive_conclusion.priority_directions),
        'references': len(data.appendices.reference_literature),
    }


def main(argv=None):
    from modules.report.evaluation_presentation import reader_quality
    from modules.report.evaluation_schema import EvaluationData

    paths = list(sys.argv[1:] if argv is None else argv)
    if not paths:
        raise SystemExit(__doc__)
    failed = False
    for path in paths:
        body = json.loads(Path(path).read_text(encoding='utf-8'))
        data = EvaluationData.model_validate(body.get('data') or {})
        quality = reader_quality(data)
        quality['sections'] = _sections(data)
        hard = (
            quality['otherPartyMentions']
            + quality['captureMarkers']
            + quality['englishCodes']
            + quality['secondPerson']
        )
        failed |= hard > 0
        print(json.dumps({'report': path, **quality}, ensure_ascii=False))
    raise SystemExit(1 if failed else 0)


if __name__ == '__main__':
    main()
