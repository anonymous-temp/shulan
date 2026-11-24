"""Evaluate-only recovery of literal numeric overview cells from one PDF."""
from __future__ import annotations


_CODES = frozenset({'microbiome_health_score', 'gut_overall_score',
    'gut_immunity', 'anti_inflammatory', 'anti_inflammatory_capacity',
    'be_ratio', 'fb_ratio', 'be_score', 'fb_score', 'dietary_fiber_need'})


def _complete_cells(rows):
    """Drop a repeated summary only when its cells are a literal subset.

    The detailed patient table can supply the same value and status plus its
    reference. Competing values, spellings or grades remain separate facts.
    """
    fields = ('value', 'unit', 'referenceRange', 'status')
    return [row for row in rows if not any(other is not row
        and other['metricCode'] == row['metricCode']
        and all(row.get(key) is None or row.get(key) == other.get(key) for key in fields)
        and any(row.get(key) is None and other.get(key) is not None for key in fields)
        for other in rows)]


def attach_original_pdf_scores(parsed, document):
    """Use this selected original's result cells, never a derived score.

    Some GM/G1 tables use ``肠道状态`` rather than the generic reader's
    status heading. The strict table reader already understands these columns;
    only evaluate consumes this repair. JSON sources never enter this hook.
    Printed grades take precedence even at a printed reference boundary.
    """
    if (document is None or parsed.source_name != document.filename
            or not parsed.source_checksum
            or parsed.source_checksum != document.metadata.get('content_sha256')):
        return parsed
    from .document_source import strict_report_source_records
    from .evaluation_template_pdf import EvaluationTemplatePdfStructureMetric
    from .evaluation_disease_catalog import attach_source_catalog

    parsed = attach_source_catalog(parsed, document)
    originals = [row for row in strict_report_source_records(document)
        if row.get('metricCode') in _CODES and type(row.get('sourcePage')) is int
        and row['sourcePage'] > 0]
    # Positioned score labels and their own result columns can survive even
    # when PDF table extraction splits or merges the printed cells. They carry
    # the source pages of the exact geometry reader, not a nearby legend.
    for metric in document.native_metrics:
        pages = getattr(metric, 'source_pages', ())
        if metric.code not in _CODES or not pages or metric.value is None:
            continue
        page = getattr(metric, 'source_page', None)
        if type(page) is not int or page not in pages or page <= 0:
            continue
        reference = metric.reference_range
        if reference in {'-', '—', '/', '／'}:
            reference = None
        originals.append(dict(metricCode=metric.code, itemName=metric.name,
            value=metric.value, unit=metric.unit, referenceRange=reference,
            status=metric.status, sourcePage=page, sourcePages=list(pages),
            reportTime=parsed.sample.report_date, sourceSection=None,
            sourceText=' | '.join(str(value) for value in
                (metric.name, metric.value, metric.unit, metric.reference_range, metric.status)
                if value is not None)))
    originals = _complete_cells(originals)
    if not originals:
        return parsed
    output = parsed.model_copy(deep=True)
    recovered = {row['metricCode'] for row in originals}
    def belongs(item):
        filename = getattr(item, 'source_file', None)
        checksum = getattr(item, 'source_checksum', None)
        if hasattr(item, 'model_extra') and item.model_extra:
            filename = item.model_extra.get('sourceFile', item.model_extra.get('source_file', filename))
            checksum = item.model_extra.get('sourceChecksum', item.model_extra.get('source_checksum', checksum))
        return filename in (None, document.filename) and checksum in (None, parsed.source_checksum)

    output.metrics = [row for row in output.metrics if row.code not in recovered or not belongs(row)]
    # Preserve all auxiliary source facts, including a categorical summary.
    # Their table-cell roles, rather than deletion, control display projection.
    seen = set()
    for original in originals:
        identity = tuple(original.get(key) for key in ('metricCode', 'value', 'unit',
            'referenceRange', 'status', 'sourcePage'))
        if identity in seen:
            continue
        seen.add(identity)
        output.metrics.append(EvaluationTemplatePdfStructureMetric(
            code=original['metricCode'], name=original['itemName'], value=original['value'],
            unit=original['unit'], referenceRange=original['referenceRange'], status=original['status'],
            sourcePage=original['sourcePage'], sourceFile=document.filename,
            sourceChecksum=parsed.source_checksum, observedAt=original['reportTime'],
            sourceSection=original.get('sourceSection'), sourceText=original['sourceText']))
    output._evaluation_source_audit = [row for row in getattr(parsed, '_evaluation_source_audit', [])
        if row.get('kind') != 'selected_pdf_numeric_cells'] + [dict(
            kind='selected_pdf_numeric_cells', sourceFile=document.filename,
            sourceChecksum=parsed.source_checksum, originalRows=originals)]
    return output
