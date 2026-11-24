"""Pure fixtures, quality checks and memory doubles for analysis benchmarks."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


class MemoryTrace:
    def __init__(self, raw, name):
        self.parsed_document = raw
        self.trace_id = name
        self.payloads = {}

    async def write_payload(self, name, payload):
        self.payloads[name] = deepcopy(payload)

    async def read_payload(self, name):
        return deepcopy(self.payloads.get(name))

    async def update(self, **fields):
        pass



class NoDocMind:
    def submit_online_file(self, *args, **kwargs):
        raise AssertionError('benchmark must not submit real documents')

    def get_result(self, *args, **kwargs):
        raise AssertionError('benchmark must not query DocMind')



def scenarios():
    archived = json.loads((ROOT / 'tests/fixtures/analysis/docmind_bilingual_spans.json').read_text(encoding='utf-8'))
    dense = '|项目|结果|参考范围|单位|结果评价|\n|---|---|---|---|---|\n'
    dense += ''.join(f'|合成指标{i}|{i}|0-200|%|正常|\n' for i in range(120))
    mixed = (
        '合成检验报告，检测日期2026-01-01\n'
        '|项目|结果|检测日期|标本|\n|---|---|---|---|\n|项目A|阴性|2026-01-01|血清|\n'
        '另一份合成检验报告，检测日期2026-02-01\n'
        '|项目|结果|检测日期|标本|\n|---|---|---|---|\n|项目A|未检出|2026-02-01|血浆|\n'
        '合成超声报告，报告日期2026-02-02。影像所见：肝脏回声增强。影像诊断：脂肪肝。\n'
    )
    return {
        'bilingual_spans': archived,
        'dense_120': {'success': True, 'text': dense},
        'mixed_dates_imaging': {'success': True, 'text': mixed},
    }



def _quality(result, records, *, imaging):
    if not isinstance(result, dict):
        return False
    actual = result.get('observations', [])
    if len(actual) != len(records):
        return False
    remaining = list(actual)
    for record in records:
        matches = [
            item
            for item in remaining
            if item.get('itemName') in record.names
            and str(item.get('resultValue')) == record.value
            and all(item.get(key) == value for key, value in record.fields.items())
        ]
        if len(matches) != 1:
            return False
        remaining.remove(matches[0])
    if imaging:
        sections = result.get('sections') or [result]
        if not any(
            isinstance(section.get('expansion'), dict)
            and str(section['expansion'].get('findings', '')).strip().rstrip('。.') == '肝脏回声增强'
            and str(section['expansion'].get('diagnosis', '')).strip().rstrip('。.') == '脂肪肝'
            and section.get('reportTime') == '2026-02-02'
            for section in sections
        ):
            return False
    return not remaining
