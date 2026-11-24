"""Offline case preparation/replay. Never loads configuration or calls services."""

import argparse
import json

from tests.unit._support.harnesses.questionnaire_factor_case import build_case
from tests.unit._support.harnesses.questionnaire_factor_case import output_directory
from tests.unit._support.harnesses.questionnaire_factor_case import read_json
from tests.unit._support.harnesses.questionnaire_factor_case import write_outputs


def input_parser(description):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('--report-json', required=True)
    parser.add_argument('--questionnaire-json', required=True)
    parser.add_argument('--answers-xlsx', required=True)
    parser.add_argument('--sheet')
    parser.add_argument('--answer-row', type=int, default=2)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--catalog', default='tests/api/scripts/questionnaire_factor_catalog.json')
    return parser


def case_from_args(args):
    return build_case(
        args.report_json, args.questionnaire_json, args.answers_xlsx, sheet=args.sheet, answer_row=args.answer_row
    )


def main():
    parser = input_parser(
        'Prepare inputs or replay an explicitly supplied analysis; no medical inference is simulated.'
    )
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--analysis')
    parser.add_argument('--knowledge')
    args = parser.parse_args()
    case = case_from_args(args)
    if args.prepare:
        path = output_directory(args.output_dir)
        path.mkdir(parents=True, exist_ok=True)
        with (path / 'case-input.json').open('x', encoding='utf-8') as stream:
            json.dump(case, stream, ensure_ascii=False, indent=4)
        print('Prepared case-input.json; no model or knowledge service called.')
        return
    if not args.analysis or not args.knowledge:
        parser.error('replay requires --analysis and --knowledge; use --prepare to inspect input only')
    write_outputs(
        args.output_dir,
        case,
        read_json(args.analysis),
        read_json(args.catalog),
        read_json(args.knowledge),
        mode='assistant_analysis_replay',
        telemetry={'modelCalls': 0, 'knowledgeCalls': 0},
    )
    print('Replay validated; wrote result.json, comparison.md and comparison.html. Medical review remains pending.')


if __name__ == '__main__':
    main()
