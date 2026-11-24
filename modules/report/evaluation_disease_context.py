"""Exact native association evidence for fixed disease questions.

A negative association screen is not an excluded clinical disease. An
association result supplies no numeric model score or disease diagnosis.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
import unicodedata

from .evaluation_native_modules import (native_disease_associations,
    native_association_result, native_association_positive)
from . import evaluation_catalog as catalog


# Group the assay's exact clinical theme, never its result as a diagnosis.
# Child aliases do not inherit the adult AD/IBS meanings by substring.
_CATEGORY_KEYS = {code: set() for code, _ in catalog.RISK_CATEGORY_DEFINITIONS}
for _rows in catalog.SOURCE_ASSOCIATION_DIRECTORY.values():
    for _group, _panel, _title, _model in _rows:
        _CATEGORY_KEYS[catalog.disease_category(_title,
            code='nativeAssociation:' + _panel, category=_group)].add(_panel)
_UNCLASSIFIED_ASSOCIATION_KEYS = frozenset()



def category_associations(data, category):
    return [entry['row'] for entry in native_disease_associations(data)
        if entry['key'] in _CATEGORY_KEYS.get(category, ())]


def association_positive(row):
    return native_association_positive(row)


def _signal_key(signal):
    name = signal.scientific_name or signal.name or ''
    name = re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', name)).casefold()
    value = str(signal.value)
    try:
        parsed = Decimal(value)
        if parsed.is_finite():
            value = format(parsed.normalize(), 'f')
    except InvalidOperation:
        pass
    return name, value, signal.unit, signal.source_status


def _merge_signal(owner, signal):
    dates = {source.observed_at for source in signal.sources if source.observed_at}
    for old in owner.related_taxa:
        previous_dates = {source.observed_at for source in old.sources if source.observed_at}
        if _signal_key(old) != _signal_key(signal) or dates and previous_dates and dates != previous_dates:
            continue
        for source in signal.sources:
            if source not in old.sources:
                old.sources.append(source.model_copy(deep=True))
        return
    owner.related_taxa.append(signal.model_copy(deep=True))


def _native_member_signals(row, data, panel):
    """Exact original table members; zero/normal do not mean not detected.

    The summary's signed source locator, not an organism description or disease
    name substring, owns membership.  Two different PDFs/dates cannot lend
    their measured taxa to each other.
    """
    from .evaluation_schema import TaxonSignal
    scopes = set()
    for source in row.sources:
        match = re.fullmatch(r'source:([^:]+):(?:json:|page:\d+:template:)'
            + re.escape(panel) + r'_all_result', str(source.record_id or ''))
        if match and source.source_code == 'C':
            scopes.add((match[1], source.observed_at))
    result = []
    for member in data.appendices.source_items:
        if member.kind not in {'taxon', 'microbe'} or member.value is None:
            continue
        matched = []
        for source in member.sources:
            match = re.fullmatch(r'source:([^:]+):(?:json:|page:\d+:template:)'
                + re.escape(panel) + r'\[\d+\]', str(source.record_id or ''))
            if match and source.source_code == 'C' and any(match[1] == checksum
                    and (not date or not source.observed_at or date == source.observed_at)
                    for checksum, date in scopes):
                matched.append(source)
        if not matched:
            continue
        signal = TaxonSignal(name=member.name, scientific_name=member.scientific_name,
            value=member.value, unit=member.unit, source_status=member.source_status,
            sources=[source.model_copy(deep=True) for source in member.sources])
        # Keep separate dated/source occurrences; equal mirrors merge only
        # through the same precise value/status/unit and source date scope.
        if not any(_signal_key(old) == _signal_key(signal) and old.sources == signal.sources for old in result):
            result.append(signal)
    return result


def attach_native_member_evidence(item, data, panel):
    """Use the original abnormal list, else the actual normal member table."""
    entries = [entry for entry in native_disease_associations(data) if entry['key'] == panel]
    if not entries:
        return
    for entry in entries:
        row = entry['row']
        signals = row.related_taxa or _native_member_signals(row, data, panel)
        for signal in signals:
            _merge_signal(item, signal)
    # Empty is truthful when this exact original module has no member table;
    # no generic IBD row is borrowed for a narrower disease model.


def attach_exact_association(item, data, code):
    """Augment an actual model's evidence, or fill only a missing qualitative slot."""
    entries = native_disease_associations(data, code)
    if not entries:
        return
    rows = [entry['row'] for entry in entries]
    for entry in entries:
        attach_native_member_evidence(entry['row'], data, entry['key'])
    existing = list(item.related_taxa)
    item.related_taxa = []
    for signal in existing:
        _merge_signal(item, signal)
    for row in rows:
        for source in row.sources:
            if source not in item.sources:
                item.sources.append(source.model_copy(deep=True))
        for signal in row.related_taxa:
            _merge_signal(item, signal)
    if item.value is None and item.assessment_level is None:
        # This is explicitly a qualitative assay state, not a risk grade.
        item.assessment_level = ('检出相关菌群异常' if any(association_positive(row) for row in rows)
            else '未见相关菌群异常')
        item._assessment_origin = 'report'
    state_lines = [row.name + '：' + native_association_result(row)
        for row in rows if native_association_result(row)]
    if not item.explanation or item.explanation == '；'.join(state_lines):
        item.explanation = '；'.join(dict.fromkeys(state_lines)) or None
    item.availability = 'partial'


def native_category_basis(data, category):
    rows = category_associations(data, category)
    if not rows:
        return None, None, []
    positives = [row for row in rows if association_positive(row)]
    selected = positives or rows
    basis = '；'.join(row.name + '：' + native_association_result(row)
        for row in selected if native_association_result(row))
    sources = []
    for row in rows:
        for source in row.sources:
            if source not in sources:
                sources.append(source.model_copy(deep=True))
    return ('注意' if positives else '未见异常关联信号'), basis, sources


def clinical_attention_basis(data, rows):
    """The same QA mirrors are source bindings, not repeated clinical evidence."""
    reported = [row for row in rows if row.kind == 'self_reported']
    ids = {source.record_id for row in reported for source in row.sources}
    symptoms = [row for row in data.medical_assessment.findings if row.certainty == 'self_reported'
        and row.value is not None and ids.intersection(source.record_id for source in row.sources)]
    parts = []
    if symptoms:
        parts.append('本人自述：' + '、'.join(dict.fromkeys(str(row.value).rstrip('。；;') for row in symptoms)))
    for row in rows:
        if row.kind == 'self_reported':
            if symptoms:
                continue
            value = re.sub(r'^问题：.*?；回答：', '', str(row.value or ''), flags=re.S)
            parts.append('本人自述：' + value)
        else:
            parts.append(row.name + '：' + str(row.value))
    return '；'.join(dict.fromkeys(parts))
