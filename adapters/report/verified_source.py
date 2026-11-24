"""Conservative Report projection over observations already accepted by Analysis."""

from __future__ import annotations

import json
import re
import unicodedata
from copy import deepcopy
from decimal import Decimal

from modules.report import parsing
from modules.report.document_source import is_scale_label
from modules.report.document_source import metric_identity
from modules.report.document_source import printed_table_taxa
from modules.report.document_source import evaluation_panel_summary_metrics
from modules.report.document_source import evaluation_amr_class_rows
from modules.report.document_source import strict_report_source_records
from modules.report.document_source import strict_report_source_diseases
from modules.report.document_source import _strict_source_metrics, _strict_hospital_rows, _is_replaced_source_row
from modules.report.source_context import source_context_records, source_enterotype

_NUMBER = re.compile(r'^([<>]=?|[≤≥=])?\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([^\d\s].*)?$')
_FORMATTED_NUMBER = re.compile(
    r'^([<>]=?|[≤≥=])?\s*([+-]?\d+(?:,\d+)+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*([^\d\s].*)?$'
)
_CJK = re.compile(r'[\u3400-\u9fff]')
_SCFA_CODES = {'formate', 'acetate', 'propionate', 'butyrate', 'valerate'}
_NATIVE_MIRROR_CODES = _SCFA_CODES | {'oxidative_stress_tolerance'}
_STRICT_NATIVE_ADDITIONS = _NATIVE_MIRROR_CODES | {'dietary_fiber_need'}


def _text(value):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ''
    return ' '.join(unicodedata.normalize('NFKC', str(value)).split())


def _names(value):
    name = _text(value)
    values = {name.casefold()} if name else set()
    bilingual = re.fullmatch(r'(.+?)\s*\(([^()]+)\)', name)
    if bilingual:
        left, right = (part.strip() for part in bilingual.groups())
        # Only an explicitly printed bilingual alias, never a substring hit.
        if bool(_CJK.search(left)) != bool(_CJK.search(right)):
            values.update((left.casefold(), right.casefold()))
    return values


def _native_metric_names(row):
    name = _text(row.get('name')).replace(' ', '').casefold()
    names = {name, re.sub(r'评估$', '', name)}
    if row.get('code') in _SCFA_CODES:
        names.add(name + '合成能力')
    return names - {''}


def _native_scalar_matches(value, unit, original):
    left = _value(value, unit or original.get('unit'))
    return left is not None and left == _value(original.get('value'), original.get('unit') or unit)


def _value(value, unit, *, confirmed_flag=None):
    text = _text(value)
    if not text:
        return None
    allowed_arrow = '↑' if confirmed_flag == '偏高' else '↓' if confirmed_flag == '偏低' else None
    if allowed_arrow and text.endswith(allowed_arrow):
        text = text[:-1].strip()
    unit = _text(unit).replace(' ', '')
    if match := _FORMATTED_NUMBER.fullmatch(text) or _NUMBER.fullmatch(text):
        comparison, number, suffix = match.groups()
        suffix = _text(suffix).replace(' ', '')
        if suffix and unit and suffix != unit:
            return None
        comparison = {'≤': '<=', '≥': '>=', '=': ''}.get(comparison, comparison or '')
        if ',' in number:
            # Preserve locale-specific punctuation literally. Do not infer
            # whether the comma is a decimal or thousands separator.
            return 'formatted_number', comparison, number, suffix or unit
        return 'number', comparison, Decimal(number), suffix or unit
    return 'text', text.casefold(), unit


def _observations(outcomes):
    rows = {}

    def visit(report, inherited_type=None):
        if not isinstance(report, dict):
            return
        items = report.get('observations')
        if not isinstance(items, list):
            items = report.get('expansion') if isinstance(report.get('expansion'), list) else []
        for original in items:
            if (
                not isinstance(original, dict) or not _names(original.get('itemName'))
                or is_scale_label(original.get('resultValue'))
            ):
                continue
            item = {
                key: deepcopy(original.get(key))
                for key in ('itemName', 'resultValue', 'unit', 'referenceRange', 'flag', 'kind', 'observedAt')
            }
            item['sourceReportType'] = report.get('reportType') or inherited_type
            if not item['observedAt'] and 'sourceSection' not in original:
                item['observedAt'] = report.get('reportTime')
            if _value(item['resultValue'], item['unit'], confirmed_flag=item['flag']) is not None:
                # Identical mirrors are one fact; different dates/units/metadata stay distinct.
                rows.setdefault(json.dumps(item, sort_keys=True, ensure_ascii=False), item)
        for section in report.get('sections') or []:
            visit(section, report.get('reportType') or inherited_type)

    accepted = {}
    for outcome in outcomes:
        rows = {}
        visit(outcome.report)
        mirrors = {}
        for item in rows.values():
            key = json.dumps({k: v for k, v in item.items() if k != 'sourceReportType'}, sort_keys=True, ensure_ascii=False)
            mirrors.setdefault(key, []).append(item)
        for group in mirrors.values():
            known = [item for item in group if item['sourceReportType'] not in (None, '', 'unknown')]
            # The same document may repeat a fact at the root and in an
            # unclassified child section. Missing type is not a conflicting
            # scalar. Distinct known types/values/units/dates remain ambiguous.
            retained = known if len(known) == 1 else group
            for item in retained:
                accepted.setdefault(json.dumps(item, sort_keys=True, ensure_ascii=False), item)
    return list(accepted.values())


def exact_source_projection(data, documents):
    """Repair sensitive candidates using this exact original's explicit cells.

    Shared accepted output can still contain a shifted chart value or reference
    column. This check therefore applies to complete and partial interpretations.
    It never promotes another file, a substring hit or an inferred clinical fact.
    """
    if not isinstance(data, dict):
        return data
    checksum, filename = data.get('sourceChecksum'), data.get('sourceName')
    originals = [doc for doc in documents if checksum and filename
        and doc.filename == filename and doc.metadata.get('content_sha256') == checksum]
    records = [row for doc in originals for row in strict_report_source_records(doc)]
    diseases = [row for doc in originals for row in strict_report_source_diseases(doc)]
    native_amr = [{**row, 'reportTime': parsing._explicit_report_date(doc)}
        for doc in originals for row in parsing.original_amr_table_rows(doc)]
    native_taxa, occurrences = [], []
    for doc in originals:
        native_taxa.extend(row.model_dump(mode='json', by_alias=True)
            for row in printed_table_taxa(doc, occurrences=occurrences))
    context = [row for doc in originals for row in source_context_records(doc)]
    if not records and not diseases and not native_amr and not native_taxa and not context:
        return data
    result = deepcopy(data)
    if originals:
        values = {value for doc in originals if (value := source_enterotype(doc))}
        # Competing source labels stay empty, never selected by page order.
        result['enterotype'] = next(iter(values)) if len(values) == 1 else None
        result['sourceTaxonOccurrences'] = occurrences
    if context:
        names = {row['itemName'] for row in context}
        equivalent_names = names | {'菌群关联筛查：' + name for name in names}
        equivalent_names |= {name + suffix for name in names for suffix in ('（检出基因）', '（候选基因）')}
        # Replace legacy state-only mirrors from this exact source, rather
        # than publishing both a generic status row and the original details.
        preserved = []
        for raw in result.get('supplementaryHospitalData', []):
            same_owner = raw.get('sourceFile') in (None, filename) and raw.get('sourceChecksum') in (None, checksum)
            if same_owner and raw.get('sourceReportType') in (None, 'microbiome') and raw.get('itemName') in equivalent_names:
                continue
            preserved.append(raw)
        preserved.extend(context)
        result['supplementaryHospitalData'] = preserved
    if native_taxa:
        # The intrinsic profile can have folded repeated organisms by ID.
        # Preserve explicit source ranges/flags, including contradictory rows,
        # under both complete and partial processing. Never select a flag by
        # severity or table order from this same original.
        names = {_text(row['latinName']).casefold() for row in native_taxa}
        rows = [row for row in result.get('taxa', []) if _text(row.get('latinName')).casefold() not in names]
        rows.extend(native_taxa)
        result['taxa'] = list({json.dumps(row,ensure_ascii=False,sort_keys=True): row for row in rows}.values())
    if records:
        metrics = _strict_source_metrics(records)
        codes = {row.code for row in metrics}
        result['metrics'] = [row for row in result.get('metrics', []) if row.get('code') not in codes]
        result['metrics'].extend(row.model_dump(mode='json', by_alias=True) for row in metrics)
        preserved = []
        for raw in result.get('supplementaryHospitalData', []):
            row = parsing.HospitalDataInput.model_validate(raw)
            # Only this file's microbiome candidates are replaceable. A caller's
            # clinical observation with a similar label retains its own source.
            owner = row.model_extra or {}
            if owner.get('sourceFile') not in (None, filename) or owner.get('sourceReportType') not in (None, 'microbiome'):
                preserved.append(raw)
                continue
            identity = metric_identity(row.item_name)
            if not _is_replaced_source_row(row, records) and not (identity and identity[0] in codes):
                preserved.append(raw)
        preserved.extend(row.model_dump(mode='json', by_alias=True) for row in _strict_hospital_rows(records))
        # Summary/detail mirrors with identical provenance remain a single row.
        result['supplementaryHospitalData'] = list({
            json.dumps(row, ensure_ascii=False, sort_keys=True): row for row in preserved
        }.values())
    if native_amr:
        # This named summary refers to abnormal genes, not absence of every
        # measured resistance gene. Keep the actual named source table rows.
        result['metrics'] = [row for row in result.get('metrics', [])
            if not (_text(row.get('name')) == '抗生素耐药基因' and row.get('value') == '未检出')]
        original_names = {row['itemName'] for row in native_amr}
        rows = [row for row in result.get('supplementaryHospitalData', [])
            if row.get('itemName') not in original_names and not (
                row.get('itemName') == '抗生素耐药基因' and (row.get('examResult') or row.get('testResultValue')) == '未检出')]
        for row in native_amr:
            rows.append({'dataType':'EXAM','itemName':row['itemName'],'examResult':row['value'],
                'unit':row['unit'],'referenceRange':row.get('referenceRange'),
                'resultStatusName':row['status'],'sourceReportType':'microbiome','sourceFile':filename,
                'sourceCategory':'antimicrobialResistance','sourceChecksum':checksum,
                'reportTime':row['reportTime']})
        result['supplementaryHospitalData'] = rows
    if diseases:
        # Complete source risk cells are the authority for this file. Neither
        # a paragraph nor the next row's scalar can create an additional model.
        result['diseaseResults'] = list({row.model_dump_json(): row.model_dump(mode='json', by_alias=True)
            for row in diseases}.values())
        # A prose/card mirror labelled "<model>风险值" is the same model
        # score, not a metabolism measurement merely because its name also
        # contains a nutrient word. The explicit risk rows above retain it.
        model_labels = {_text(row.name).replace(' ', '').casefold() + suffix
            for row in diseases for suffix in ('风险值','风险指数','riskvalue','riskscore')}
        result['supplementaryHospitalData'] = [row for row in result.get('supplementaryHospitalData', [])
            if row.get('sourceFile') not in (None,filename)
            or row.get('sourceReportType') not in (None,'microbiome')
            or _text(row.get('itemName')).replace(' ', '').casefold() not in model_labels]
    return result


def trusted_report_source(data, outcomes, *, table_documents=()):
    """Project accepted scalars; restore metadata only from original table rows.

    A partial AI interpretation is not a rejection of every original table.
    These documents come from the same permission-checked source, not model
    prose. Blank text disables prose/card extraction for this verification.
    """
    observations = _observations(outcomes)
    matched = []
    table_taxa, table_diseases = [], []
    clinical_originals = []
    original_metrics = []
    status_verified_metrics = []
    amr_originals = []
    nutrient_originals = []
    native_samples = []
    evaluation_panel_summaries = []
    evaluation_amr_classes = []
    for document in table_documents:
        exact_file = (_text(data.get('sourceName')) == _text(document.filename)
            and bool(data.get('sourceChecksum'))
            and document.metadata.get('content_sha256') == data.get('sourceChecksum'))
        tables_only = parsing.ExtractedDocument(document.filename, document.source_type, '', document.tables)
        if exact_file:
            table_taxa.extend(item.model_dump(mode='json', by_alias=True) for item in printed_table_taxa(tables_only))
            table_diseases.extend(
            # Disease score layouts may be printed as labelled line triplets
            # rather than geometric tables. The parser is section-anchored
            # and returns only source scores/levels, never prose diagnoses.
                item.model_dump(mode='json', by_alias=True) for item in strict_report_source_diseases(document)
            )
        # Original positioned clinical cells are scoped to this exact file.
        # They only repair an already accepted observation below; they do not
        # admit unrelated prose or manufacture extra clinical measurements.
        original_checksum = document.metadata.get('content_sha256')
        same_checksum = not original_checksum or not data.get('sourceChecksum') or original_checksum == data['sourceChecksum']
        if same_checksum and _text(data.get('sourceName')) == _text(document.filename):
            clinical_originals.extend(parsing.original_clinical_table_rows(document))
            original_metrics.extend(item.model_dump(mode='json', by_alias=True) for item in document.native_metrics)
            if original_checksum and original_checksum == data.get('sourceChecksum'):
                evaluation_panel_summaries.extend(evaluation_panel_summary_metrics(document))
                evaluation_amr_classes.extend(evaluation_amr_class_rows(document))
                status_verified_metrics.extend(item.model_dump(mode='json', by_alias=True) for item in document.native_metrics)
                original_date = parsing._explicit_report_date(document)
                amr_originals.extend({**item, 'reportTime': original_date}
                    for item in parsing.original_amr_table_rows(document))
                nutrient_originals.extend({**item, 'reportTime': original_date}
                    for item in parsing.original_nutrient_synthesis_rows(document))
                native_samples.append(parsing.original_sample_metadata(document))

    # Repeated summary/detail pages are one identical scalar, while genuinely
    # conflicting native values or labels cannot be selected by page order.
    nutrient_groups = {}
    for original in nutrient_originals:
        name = _text(original['itemName']).replace(' ', '').casefold()
        identity = {k: v for k, v in original.items() if k != 'sourcePage'}
        nutrient_groups.setdefault(name, {}).setdefault(
            json.dumps(identity, sort_keys=True, ensure_ascii=False), original)
    unique_nutrients = [next(iter(group.values())) for group in nutrient_groups.values() if len(group) == 1]

    def original_nutrient_match(item):
        if item.get('sourceReportType') != 'microbiome':
            return None
        name = _text(item['itemName']).replace(' ', '').casefold()
        return next((original for original in unique_nutrients
            if name == _text(original['itemName']).replace(' ', '').casefold()
            and _value(item['resultValue'], item['unit']) == _value(original['value'], original['unit'])), None)

    def amr_aliases(original):
        compact = lambda value: _text(value).replace(' ', '').casefold()
        return {compact(original['itemName']), compact(original['antibioticClass'] + original['itemName'])}

    def original_amr_match(item):
        if item.get('sourceReportType') != 'microbiome':
            return None
        compact = lambda value: re.sub(r'\s+', '', _text(value)).casefold()
        name, value = compact(item['itemName']), _value(item['resultValue'], item['unit'])
        if not name or value is None:
            return None
        candidates = {}
        for original in amr_originals:
            # Both aliases are literal cells in this row. The combined form
            # handles an accepted mirror that joined a merged class label to
            # the actual item; no antibiotic dictionary guesses an A/B class.
            if name in amr_aliases(original) and value == _value(original['value'], original['unit']):
                candidates.setdefault(json.dumps(original, sort_keys=True, ensure_ascii=False), original)
        return next(iter(candidates.values())) if len(candidates) == 1 else None

    def original_microbiome_metadata(item):
        # An accepted AI mirror can mislabel an explicit native table cell.
        # Correct only this label, within the exact file/checksum and scalar;
        # absence, aliases, unit conflicts or competing native rows are not
        # evidence that an existing source label should be replaced.
        if item.get('sourceReportType') != 'microbiome':
            return None
        name = _text(item['itemName']).replace(' ', '').casefold()
        candidates = {}
        for original in status_verified_metrics:
            if (name in _native_metric_names(original)
                    and _native_scalar_matches(item['resultValue'], item['unit'], original)):
                candidates.setdefault(json.dumps(original, sort_keys=True, ensure_ascii=False), original)
        if len(candidates) != 1:
            return None
        return next(iter(candidates.values()))

    def original_clinical_match(item):
        if item.get('sourceReportType') == 'microbiome':
            return None
        candidates = {}
        for original in clinical_originals:
            extra = original.model_extra or {}
            literal_value = original.test_result_value
            direct = (_names(item['itemName']) == _names(original.item_name)
                and _value(item['resultValue'], item['unit']) == _value(literal_value, extra.get('unit')))
            shifted = (_names(item['itemName']) == _names(literal_value)
                and _value(item['resultValue'], item['unit']) == _value(original.reference_range, None))
            if not direct and not shifted:
                continue
            key = (original.item_name, literal_value, extra.get('unit'), original.report_time,
                original.reference_range, original.result_status_name)
            candidates.setdefault(key, (original, shifted and not direct))
        if len(candidates) > 1 and item.get('observedAt') and all(not value[1] for value in candidates.values()):
            candidates = {key: value for key, value in candidates.items()
                if _text(value[0].report_time) == _text(item['observedAt'])}
        return next(iter(candidates.values())) if len(candidates) == 1 else None

    def table_match(row, rows, names, value_field, unit_field=None):
        candidates = {}
        for original in rows:
            if not all(_names(row.get(key)) == _names(original.get(key)) for key in names):
                continue
            unit = row.get(unit_field) if unit_field else None
            original_unit = original.get(unit_field) if unit_field else None
            if _value(row.get(value_field), unit) != _value(original.get(value_field), original_unit):
                continue
            candidates.setdefault(json.dumps(original, sort_keys=True, ensure_ascii=False), original)
        return next(iter(candidates.values())) if len(candidates) == 1 else None

    def match(row, name_fields, value, unit=None, *, printed_unit=False):
        names = set().union(*(_names(row.get(field)) for field in name_fields))
        if not names:
            return None
        identity = metric_identity(row.get('name')) if name_fields == ('name',) else None
        metric_code = identity[0] if identity and row.get('code') == identity[0] else None
        candidates = [
            item
            for item in observations
            if (names.intersection(_names(item['itemName'])) or (
                metric_code and (matched_identity := metric_identity(item['itemName']))
                and metric_code == matched_identity[0]
            ))
            and _value(value, unit, confirmed_flag=item['flag']) is not None
            and _value(value, unit, confirmed_flag=item['flag'])
            == _value(
                item['resultValue'],
                item['unit'] or (unit if printed_unit else None),
                confirmed_flag=item['flag'],
            )
        ]
        if (len(candidates) > 1 and all(isinstance(item['kind'], str) for item in candidates)
                and {item['kind'] for item in candidates} == {'assessment', 'measurement'}):
            # The shared public projection can mirror the same accepted number
            # as both a measurement and an assessment. That label alone does
            # not contradict the scalar; retain the more cautious assessment
            # classification. All other metadata must be byte-for-byte equal.
            identities = {json.dumps({k: v for k, v in item.items() if k != 'kind'},
                sort_keys=True, ensure_ascii=False) for item in candidates}
            if len(identities) == 1 and _value(value, unit, confirmed_flag=candidates[0]['flag'])[0] in {'number', 'formatted_number'}:
                candidates = [next(item for item in candidates if item['kind'] == 'assessment')]
        if printed_unit and len(candidates) > 1:
            # Overview/detail mirrors may omit a unit or flag. Collapse only
            # same-date scalar mirrors with no contradictory metadata.
            if len({_text(item['observedAt']) for item in candidates}) == 1 and all(
                len({_text(item[key]) for item in candidates if _text(item[key])}) <= 1
                for key in ('flag', 'referenceRange')
            ):
                return max(
                    candidates, key=lambda item: sum(bool(item[key]) for key in ('unit', 'flag', 'referenceRange'))
                )
        if len(candidates) != 1:
            return None
        return candidates[0]

    subject = data.get('subject') if isinstance(data.get('subject'), dict) else {}
    verified_subject = {}
    if name := parsing._valid_subject_name(subject.get('name')):
        verified_subject['name'] = name
    try:
        age = int(subject.get('age')) if subject.get('age') not in (None, '') else None
    except (TypeError, ValueError):
        age = None
    if age is not None and 0 <= age <= 150:
        verified_subject['age'] = age
    sex = _text(subject.get('sex'))
    if sex in {'男', '女', '未知'}:
        verified_subject['sex'] = sex

    result = {
        key: deepcopy(data[key]) for key in ('sourceType', 'sourceName', 'sourceChecksum', 'processing') if key in data
    }
    result.update(
        # ``data`` is produced by Report's deterministic extractor over the
        # permission-checked source text. Preserve only validated identity
        # scalars; inferred prose and model summaries remain excluded.
        subject=verified_subject,
        sample={},
        metrics=[],
        taxa=[],
        sourceMicrobiomeTerms=[],
        diseaseResults=[],
        panelCounts=[],
        # Ignore the unverified candidate array. Only the exact original file,
        # identified above by both filename and checksum, can restore headings.
        evaluationPanelSummaries=[item.model_dump(mode='json', by_alias=True)
            for item in evaluation_panel_summaries],
        evaluationAmrClasses=evaluation_amr_classes,
        supplementaryHospitalData=[],
        enterotype=None,
        sourceSummary=None,
    )
    for row in data.get('metrics') or []:
        item = match(row, ('name',), row.get('value'), row.get('unit'))
        if item is not None:
            matched.append(item)
            result['metrics'].append(
                {
                    'code': row['code'],
                    'name': item['itemName'],
                    'value': str(item['resultValue']),
                    'unit': item['unit'],
                    'referenceRange': item['referenceRange'],
                    'status': item['flag'],
                }
            )
    existing_codes = {row['code'] for row in result['metrics']}
    for code in dict.fromkeys(row['code'] for row in original_metrics):
        group = {}
        for row in original_metrics:
            if row['code'] == code:
                key = json.dumps({key: row.get(key) for key in ('value', 'unit', 'referenceRange', 'status')},
                    sort_keys=True, ensure_ascii=False)
                group.setdefault(key, row)
        if len(group) != 1:
            continue
        original = next(iter(group.values()))
        native_value = _value(original.get('value'), original.get('unit'))
        if native_value is None or native_value[0] not in {'number', 'formatted_number'}:
            continue
        strictly_verified = original in status_verified_metrics
        if code in existing_codes:
            # A printed unit may complete an agreed scalar, never replace a
            # non-empty unit or any other accepted value/range/status field.
            for row in result['metrics']:
                if row['code'] != code or _text(row.get('unit')) or not _text(original.get('unit')):
                    continue
                value = _value(row.get('value'), original['unit'])
                if value is not None and value == _value(original.get('value'), original['unit']):
                    row['unit'] = original['unit']
            if strictly_verified:
                for row in result['metrics']:
                    name = _text(row.get('name')).replace(' ', '').casefold()
                    if row['code'] != code or name not in _native_metric_names(original) or not _native_scalar_matches(row.get('value'), row.get('unit'), original):
                        continue
                    for field in ('status', 'referenceRange'):
                        if _text(original.get(field)) and _text(row.get(field)).replace(' ', '') != _text(original[field]).replace(' ', ''):
                            row[field] = original[field]
            continue
        if code in _STRICT_NATIVE_ADDITIONS and not strictly_verified:
            continue
        if code in _NATIVE_MIRROR_CODES:
            represented = False
            for source_row in data.get('supplementaryHospitalData') or []:
                value = source_row.get('examResult') if source_row.get('dataType') == 'EXAM' else source_row.get('testResultValue')
                item = match(source_row, ('itemName',), value, source_row.get('unit'))
                if (item is not None and item.get('sourceReportType') == 'microbiome'
                    and _text(item['itemName']).replace(' ', '').casefold() in _native_metric_names(original)
                    and _native_scalar_matches(item['resultValue'], item['unit'], original)):
                    represented = True
                    break
            if represented:
                continue  # Preserve the existing source row, not a duplicate canonical scalar.
        result['metrics'].append(deepcopy(original))
        existing_codes.add(code)
    for row in data.get('taxa') or []:
        printed = table_match(row, table_taxa, ('latinName', 'chineseName'), 'value', 'unit')
        item = match(
            row, ('latinName', 'chineseName'), row.get('value'), row.get('unit'), printed_unit=printed is not None
        )
        if item is None:
            continue
        # Classification only: a source-confirmed Chinese organism label is
        # still microbial even when bilingual identity is not proven. Do not
        # promote its value/direction into the verified taxon result.
        aliases = _names(item['itemName'])
        result['sourceMicrobiomeTerms'].extend(
            name for name in (row.get('chineseName'), row.get('latinName'))
            if name and _names(name).intersection(aliases)
        )
        if printed is not None:
            # The table, not a Chinese-name dictionary or an AI summary,
            # establishes the bilingual identity, interval and direction.
            matched.append(item)
            result['taxa'].append(deepcopy(printed))
            continue
        if not _names(row.get('latinName')).intersection(aliases):
            continue  # A Chinese match alone cannot establish a different Latin identity.
        matched.append(item)
        result['taxa'].append(
            {
                'taxonId': row['taxonId'],
                'category': 'other',
                'latinName': row['latinName'],
                'chineseName': row.get('chineseName') if _names(row.get('chineseName')).intersection(aliases) else None,
                'value': str(item['resultValue']),
                'unit': item['unit'],
                'referenceRange': item['referenceRange'],
                'sourceStatus': item['flag'],
                'explicitlyAbnormal': item['flag'] in {'偏高', '偏低', '异常'},
                'detectionStatus': 'unknown',
            }
        )
    for row in data.get('diseaseResults') or []:
        value = row.get('sourceScore') if row.get('sourceScore') is not None else row.get('sourceLevel')
        item = match(row, ('name',), value)
        if item is not None:
            matched.append(item)
            printed = table_match(row, table_diseases, ('name',), 'sourceScore')
            if printed is not None and row.get('sourceScore') is not None:
                result['diseaseResults'].append(deepcopy(printed))
                continue
            numeric = _value(item['resultValue'], item['unit'], confirmed_flag=item['flag'])[0] == 'number'
            result['diseaseResults'].append(
                {
                    'diseaseId': row['diseaseId'],
                    'name': item['itemName'],
                    'sourceScore': str(item['resultValue']) if numeric else None,
                    'sourceLevel': item['flag'] if numeric else str(item['resultValue']),
                    'sourceDescription': None,
                    'relatedTaxa': [],
                }
            )
    for row in data.get('supplementaryHospitalData') or []:
        value = row.get('examResult') if row.get('dataType') == 'EXAM' else row.get('testResultValue')
        item = match(row, ('itemName',), value, row.get('unit'))
        if item is not None:
            matched.append(item)
            narrative = item['kind'] in {'narrative', 'assessment'}
            value = str(item['resultValue'])
            if item['unit'] and not _text(value).endswith(_text(item['unit'])):
                value += f' {item["unit"]}'
            projected = {
                    'dataType': 'EXAM' if narrative else 'INSPECTION',
                    'itemName': item['itemName'],
                    'examResult' if narrative else 'testResultValue': value,
                    'reportTime': item['observedAt'],
                    'referenceRange': item['referenceRange'],
                    'resultStatusName': item['flag'],
                    'sourceReportType': item.get('sourceReportType'),
                    'sourceFile': data.get('sourceName'),
                }
            if printed := original_clinical_match(item):
                original, shifted = printed
                extra = original.model_extra or {}
                if shifted:
                    projected.pop('examResult', None)
                    projected.pop('testResultValue', None)
                    projected['dataType'] = 'INSPECTION'
                literal_value = original.test_result_value
                if extra.get('unit') and not _text(literal_value).endswith(_text(extra['unit'])):
                    literal_value += ' ' + extra['unit']
                observed_at = original.report_time or item['observedAt']
                if (original.report_time and item['observedAt']
                    and parsing._date_value(original.report_time) == parsing._date_value(item['observedAt'])
                    and not re.search(r'\d:\d', original.report_time)
                    and re.search(r'\d:\d', item['observedAt'])):
                    observed_at = item['observedAt']
                projected.update({
                    'itemName': original.item_name,
                    'examResult' if projected['dataType'] == 'EXAM' else 'testResultValue': literal_value,
                    'reportTime': observed_at,
                    'referenceRange': original.reference_range,
                    'resultStatusName': original.result_status_name,
                    'unit': extra.get('unit'),
                    'sourceReportType': item.get('sourceReportType') or 'checkup',
                    'sourceFile': extra.get('sourceFile') or extra.get('source_file') or data.get('sourceName'),
                    'sourcePage': extra.get('sourcePage') or extra.get('source_page'),
                    'sourceSection': extra.get('sourceSection') or extra.get('source_section'),
                    'sourceText': extra.get('sourceText') or extra.get('source_text'),
                })
            if (native := original_microbiome_metadata(item)) is not None:
                for field, target in (('status', 'resultStatusName'), ('referenceRange', 'referenceRange')):
                    if _text(native.get(field)) and _text(projected.get(target)).replace(' ', '') != _text(native[field]).replace(' ', ''):
                        projected[target] = native[field]
            if (amr := original_amr_match(item)) is not None:
                projected['sourceCategory'] = amr['sourceCategory']
                if _text(amr.get('status')):
                    projected['resultStatusName'] = amr['status']
            if (nutrient := original_nutrient_match(item)) is not None:
                projected['sourceCategory'] = nutrient['sourceCategory']
                projected['sourceSubcategory'] = nutrient['sourceSubcategory']
                if _text(nutrient.get('status')):
                    projected['resultStatusName'] = nutrient['status']
            result['supplementaryHospitalData'].append(projected)
    for original in unique_nutrients:
        if any(row.get('sourceReportType') == 'microbiome'
            and _text(row.get('itemName')).replace(' ', '').casefold()
                == _text(original['itemName']).replace(' ', '').casefold()
            and _value(row.get('examResult') if row.get('dataType') == 'EXAM' else row.get('testResultValue'), row.get('unit'))
                == _value(original['value'], original['unit'])
            for row in result['supplementaryHospitalData']):
            continue
        value = original['value']
        if original['unit'] and not _text(value).endswith(_text(original['unit'])):
            value += ' ' + original['unit']
        result['supplementaryHospitalData'].append({
            'dataType': 'EXAM', 'itemName': original['itemName'], 'examResult': value,
            'unit': original['unit'], 'reportTime': original['reportTime'], 'referenceRange': None,
            'resultStatusName': original['status'], 'sourceReportType': 'microbiome',
            'sourceFile': data.get('sourceName'), 'sourceCategory': original['sourceCategory'],
            'sourceSubcategory': original['sourceSubcategory'], 'sourcePage': original['sourcePage'],
        })
    # Explicit native AMR rows are source observations in their own right.
    # Restore an omitted scalar only when its complete named row is unique;
    # retain an accepted different value without overwriting or selecting it.
    native_amr_groups = {}
    for original in amr_originals:
        name = _text(original['itemName']).replace(' ', '').casefold()
        native_amr_groups.setdefault(name, {}).setdefault(
            json.dumps(original, sort_keys=True, ensure_ascii=False), original)
    for group in native_amr_groups.values():
        if len(group) != 1:
            continue
        original = next(iter(group.values()))
        aliases = amr_aliases(original)
        if any(row.get('sourceReportType') == 'microbiome'
            and _text(row.get('itemName')).replace(' ', '').casefold() in aliases
            and _value(row.get('examResult') if row.get('dataType') == 'EXAM' else row.get('testResultValue'), row.get('unit'))
                == _value(original['value'], original['unit'])
            for row in result['supplementaryHospitalData']):
            continue
        value = original['value']
        if original['unit'] and not _text(value).endswith(_text(original['unit'])):
            value += ' ' + original['unit']
        result['supplementaryHospitalData'].append({
            'dataType': 'EXAM', 'itemName': original['itemName'], 'examResult': value,
            'unit': original['unit'], 'reportTime': original['reportTime'], 'referenceRange': None,
            'resultStatusName': original['status'], 'sourceReportType': 'microbiome',
            'sourceFile': data.get('sourceName'), 'sourceCategory': original['sourceCategory'],
        })
    # Original table geometry (or exact shared OCR tables for an image/scan)
    # is source evidence in its own right. It is safer than a partial AI
    # interpretation and must remain available when every AI observation for
    # that chunk was quarantined. The callback still carries the partial
    # marker; this does not upgrade the document to complete.
    taxon_keys = {
        tuple(_text(item.get(k)).casefold() for k in ('latinName','value','unit','referenceRange','sourceStatus'))
        for item in result['taxa']
    }
    for item in table_taxa:
        key = tuple(_text(item.get(k)).casefold() for k in ('latinName','value','unit','referenceRange','sourceStatus'))
        if key not in taxon_keys:
            result['taxa'].append(deepcopy(item))
            taxon_keys.add(key)
    disease_keys = {
        (_text(item.get('name')).casefold(), _text(item.get('sourceScore')), _text(item.get('sourceLevel')))
        for item in result['diseaseResults']
    }
    for item in table_diseases:
        key = (_text(item.get('name')).casefold(), _text(item.get('sourceScore')), _text(item.get('sourceLevel')))
        if key not in disease_keys:
            result['diseaseResults'].append(deepcopy(item))
            disease_keys.add(key)
    dates = {_text(item['observedAt']) for item in matched}
    original_dates = {parsing._date_value(value): value for document in table_documents
        if (value := parsing._explicit_report_date(document))}
    if original_dates:
        # These are permission-checked original/OCR documents, not candidate
        # metadata or model prose. Do not substitute sampling dates or hide
        # conflicting accepted dates. Per-observation dates stay untouched.
        if len(original_dates) == 1 and {parsing._date_value(v) for v in dates - {''}} <= original_dates.keys():
            result['sample'] = {'reportDate': next(iter(original_dates.values()))}
    elif len(dates) == 1 and '' not in dates:
        result['sample'] = {'reportDate': dates.pop()}
    for field in parsing.SampleInfo.model_fields.values():
        key = field.alias
        values = {json.dumps(sample[key], ensure_ascii=False): sample[key]
            for sample in native_samples if sample.get(key) is not None and sample[key] != ''}
        if len(values) == 1:
            result['sample'][key] = next(iter(values.values()))
    for key in ('metrics', 'taxa', 'supplementaryHospitalData'):
        result[key] = list({json.dumps(row, sort_keys=True, ensure_ascii=False): row for row in result[key]}.values())
    return result
