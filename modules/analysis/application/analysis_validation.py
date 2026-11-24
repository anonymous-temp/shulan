"""Validate the JSON contract returned by the report LLM; never interpret source text."""

from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field
from math import isfinite

VALIDATION_MODE = 'json_schema'
REPORT_TYPES = {'lab', 'microbiome', 'imaging', 'checkup', 'unknown'}
ITEM_FIELDS = ('itemName', 'resultValue', 'referenceRange', 'unit', 'flag', 'kind', 'observedAt', 'method', 'specimen')
_OPTIONAL_FIELDS = ('referenceRange', 'unit', 'flag', 'observedAt', 'method', 'specimen', 'rawFlag')


@dataclass
class ValidationResult:
    data: dict
    issues: list[dict]
    diagnostics: list[dict] = field(default_factory=list)

    @property
    def valid(self):
        return not self.issues

    @property
    def has_core_issues(self):
        return bool(self.issues)

    @property
    def repair_ids(self):
        # JSON correction concerns the reply shape, not a source-region protocol.
        return set()

    @property
    def accepted_count(self):
        def count(node):
            return len(node.get('observations', [])) + sum(count(child) for child in node.get('sections', []))

        return count(self.data)


def validate_analysis(data, records=(), *, allow_unbound=False, units=(), direct_observations=None):
    """Only JSON fields, types and enums are checked. Legacy source arguments are ignored."""
    issues = []

    def invalid(path, expected):
        issues.append(
            {
                'code': 'invalid_json_schema',
                'path': path,
                'expected': expected,
                'sourceIds': [],
                'severity': 'core',
                'recoverable': True,
            }
        )

    def text(value, path):
        if value is None or isinstance(value, str):
            return value
        invalid(path, 'string or null')
        return None

    def scalar(value):
        return (
            isinstance(value, str)
            and bool(value.strip())
            or (type(value) is int or type(value) is float and isfinite(value))
        )

    def item(value, path, *, unresolved=False):
        if not isinstance(value, dict):
            invalid(path, 'object')
            return None
        before = len(issues)
        name, result = value.get('itemName'), value.get('resultValue')
        if unresolved and isinstance(result, str) and not result.strip():
            result = None
        if not (isinstance(name, str) and name.strip()) and not (unresolved and name is None):
            invalid(path + '.itemName', 'non-empty string')
        if not scalar(result) and not (unresolved and result is None):
            invalid(path + '.resultValue', 'non-empty string or finite number')
        candidate = {'itemName': name, 'resultValue': result}
        for key in _OPTIONAL_FIELDS:
            if key in value:
                candidate[key] = text(value[key], path + '.' + key)
        if value.get('kind') is not None:
            if value['kind'] in ('measurement', 'assessment'):
                candidate['kind'] = value['kind']
            else:
                invalid(path + '.kind', 'measurement or assessment')
        if unresolved:
            if name is None and result is None:
                invalid(path, 'at least one known field')
            candidate['missingFields'] = [key for key in ('itemName', 'resultValue') if candidate[key] is None]
            candidate['reason'] = text(value.get('reason'), path + '.reason') or '原文信息不足，部分字段未提供。'
        return candidate if len(issues) == before else None

    def visit(node, path, depth):
        if not isinstance(node, dict):
            invalid(path, 'object')
            node = {}
        report_type = node.get('reportType')
        if not isinstance(report_type, str) or report_type not in REPORT_TYPES:
            invalid(path + '.reportType', 'lab, imaging, checkup, microbiome or unknown')
            report_type = 'unknown'
        status = node.get('status', 'pending')
        if status not in ('normal', 'abnormal', 'pending'):
            invalid(path + '.status', 'normal, abnormal or pending')
            status = 'pending'
        result = {'reportType': report_type, 'status': status}
        for key in ('title', 'reportTime', 'aiSummary'):
            result[key] = text(node.get(key), path + '.' + key)
        observations = node.get('observations')
        expansion = node.get('expansion')
        if observations is None and report_type in {'lab', 'microbiome'} and isinstance(expansion, list):
            observations = expansion
        if observations is None and (report_type in {'imaging', 'checkup'} or node.get('sections')):
            observations = []
        if not isinstance(observations, list):
            invalid(path + '.observations', 'array')
            observations = []
        result['observations'] = []
        result['unresolvedItems'] = []

        def collect(values, field):
            accepted = []
            for index, value in enumerate(values):
                name = value.get('itemName') if isinstance(value, dict) else None
                raw_result = value.get('resultValue') if isinstance(value, dict) else None
                missing = raw_result is None or isinstance(raw_result, str) and not raw_result.strip()
                unresolved = isinstance(name, str) and bool(name.strip()) and missing
                parsed = item(value, f'{path}.{field}[{index}]', unresolved=unresolved)
                if parsed is not None:
                    (result['unresolvedItems'] if unresolved else accepted).append(parsed)
            return accepted

        result['observations'] = collect(observations, 'observations')
        pending = node.get('unresolvedItems', [])
        if not isinstance(pending, list):
            invalid(path + '.unresolvedItems', 'array')
            pending = []
        for index, value in enumerate(pending):
            if parsed := item(value, f'{path}.unresolvedItems[{index}]', unresolved=True):
                result['unresolvedItems'].append(parsed)
        if report_type in {'imaging', 'checkup'}:
            if expansion is None:
                expansion = {}
            if not isinstance(expansion, dict):
                invalid(path + '.expansion', 'object or null')
                expansion = {}
            keys = ('findings', 'diagnosis') if report_type == 'imaging' else ('checkupOrgName', 'checkupTime')
            result['expansion'] = {key: text(expansion.get(key), path + '.expansion.' + key) for key in keys}
        elif report_type in {'lab', 'microbiome'}:
            if isinstance(expansion, list) and expansion is not observations:
                result['expansion'] = collect(expansion, 'expansion')
            else:
                result['expansion'] = deepcopy(result['observations'])
        else:
            result['expansion'] = None
        sections = node.get('sections', [])
        if not isinstance(sections, list) or depth and sections:
            invalid(path + '.sections', 'array of non-nested subreports')
            sections = []
        if sections:
            result['sections'] = [
                visit(child, f'{path}.sections[{index}]', depth + 1) for index, child in enumerate(sections)
            ]
        return result

    return ValidationResult(visit(data, '$', 0), issues)
