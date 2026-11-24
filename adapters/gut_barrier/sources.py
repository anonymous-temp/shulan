"""Bounded, content-aware source ingestion for the independent report API.

Downloaded text is data, never an instruction. Ambiguous table rows are retained
as source issues instead of being turned into fabricated clinical observations.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import threading
from datetime import date
from decimal import ROUND_HALF_UP
from decimal import Decimal
from decimal import DecimalException
from decimal import localcontext

from adapters.analysis.document_download import HttpDocumentDownloader
from config.report_endpoints import validate_report_endpoint
from modules.analysis.application.documents import parsed_document_text
from modules.analysis.application.documents import prepare_document
from modules.gut_barrier.input_limits import MAX_STRUCTURED_SOURCE_BYTES
from modules.gut_barrier.input_limits import normalize_structured_source
from modules.gut_barrier.rules import RETIRED_MARKER_PATTERN
from modules.gut_barrier.rules import is_retired_marker_name

SOURCE_VERSION = "gut-barrier-source-v7-request-scoped-data"
MAX_DOCUMENT_PAGES = 100
MAX_DOCUMENT_TEXT = 500000
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
MAX_OCR_PAGES = 20
OCR_PAGE_TIMEOUT_SECONDS = 60
MAX_DOCUMENT_PREVIEW_PAGES = 2
MAX_DOCUMENT_PREVIEW_TEXT = 20000


class _OcrCapacityUnavailable(RuntimeError):
    """A timed-out synchronous OCR call still owns its real execution slot."""


class _PdfPageLimitExceeded(ValueError):
    """Keep bounded cover metadata for audit, never for clinical extraction."""

    def __init__(self, *, page_count, page_limit, preview_pages, preview_errors=()):
        super().__init__("PDF page limit exceeded")
        self.page_count = page_count
        self.page_limit = page_limit
        self.preview_pages = tuple(preview_pages)
        self.preview_errors = tuple(preview_errors)


ALIASES = {
    "D_LACTATE": r"D\s*[-－—]?\s*(?:乳酸|Lact(?:ic\s*Acid|ate))|D-?LA\b",
    "DAO": r"二胺氧化酶|\bDAO\b",
    "LPS": r"细菌(?:内毒素|脂多糖)|脂多糖|\bLPS\b",
}
VALUE_UNIT = re.compile(r"^\s*[:：=]?\s*(?P<value>[<>＜＞≤≥]?\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(?:[↑↓HHL]+\s*)?"
                        r"(?P<unit>mg\s*/\s*[lLdD][lL]?|[uUμµ]g\s*/\s*[lL]|"
                        r"[uUeE][uU]?\s*/\s*(?:m[lL]|[lL]))(?:\b|$)", re.I)

METRIC_NAMES = {"GMWI2": "肠菌健康指数", "Species_Num": "菌种数", "Diversity_Index": "多样性指数",
                "Probiotics_val": "原报告益生菌指标"}
_ROUNDABLE_AUXILIARY_METRICS = frozenset({"GMWI2", "Diversity_Index", "Probiotics_val"})
_METRIC_DECIMAL = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d{1,3})?$")
_MAX_METRIC_ROUNDING_DIFFERENCE = Decimal("0.005")
REFERENCE_LABEL = re.compile(r"参考(?:值|范围|区间)?|正常(?:值|范围|区间)?|\breference(?:\s+(?:range|interval|values?))?\b", re.I)
RESULT_LABEL = re.compile(r"(?:检测|检验|测定|本次)?结果|实测(?:值)?|测定值|\b(?:test\s+)?results?\b|\bmeasured(?:\s+value)?\b", re.I)
EXAMPLE_LABEL = re.compile(r"示例|例如|举例|例题|\bexamples?\b|\be\.g\.", re.I)
NON_RESULT_LABEL = re.compile(r"方法学|(?:检测|检验|测定)方法|方法介绍|方法说明|说明(?:文案)?|科普|原理|\bmethod(?:ology)?\b|\bexplanation\b", re.I)
NON_RESULT_HEADING = re.compile(
    r"^\s*(?:[一二三四五六七八九十\d]+[、.)．]\s*)?(?:示例(?:说明|报告)?|例如|举例|方法学(?:介绍|说明)?|"
    r"(?:检测|检验|测定)方法(?:介绍|说明|与原理)?|方法介绍|方法说明|说明|科普(?:说明)?|原理(?:说明)?|"
    r"examples?|methods?|methodology|explanation)\s*[:：]?\s*$", re.I)


def _cells(line, *, loose=False):
    stripped = line.strip().strip("|")
    if "|" in stripped or "\t" in stripped:
        return [value.strip() for value in re.split(r"\||\t", stripped)]
    values = re.split(r"\s{2,}", stripped)
    return re.split(r"\s+", stripped) if loose and len(values) == 1 else values


def _table_layout(line):
    """Recognize explicit columns, never infer a value from its visual position."""
    cells = _cells(line, loose=True)
    if len(cells) < 2 or not any(re.fullmatch(r"(?:检测|检验)?(?:项目|指标|名称)|item|analyte|name", cell, re.I) for cell in cells):
        return None
    results = [index for index, cell in enumerate(cells) if RESULT_LABEL.fullmatch(cell)]
    references = [index for index, cell in enumerate(cells) if REFERENCE_LABEL.fullmatch(cell)]
    if not results and not references:
        return None
    units = [index for index, cell in enumerate(cells) if re.fullmatch(r"单位|units?", cell, re.I)]
    return {"count": len(cells), "result": results[0] if len(results) == 1 else None,
            "unit": units[0] if len(units) == 1 else None}


def _result_segment(line, marker, alias, *, table=None, reference_section=False, non_result_section=False):
    """Return only an explicit result cell or a non-reference marker segment."""
    prefix, suffix = line[:marker.start()], line[marker.end():]
    # Example values remain examples even if they sit under a valid result
    # column or include the words "test result". Method prose before an analyte
    # is not a patient observation. A method note *after* a real value is fine.
    if non_result_section or EXAMPLE_LABEL.search(line) or NON_RESULT_LABEL.search(prefix):
        return None
    if table is not None:
        if REFERENCE_LABEL.search(prefix):
            return None
        cells = _cells(line, loose=True)
        if table["result"] is None:
            return None
        if len(cells) != table["count"]:
            # Lost column boundaries are ambiguous, not permission to take the
            # first number (which may be the reference range).
            return None
        result = cells[table["result"]]
        if VALUE_UNIT.match(result):
            return result
        return result + " " + cells[table["unit"]] if table["unit"] is not None else None

    suffix = re.sub(r"^\s*[（(]\s*(?:" + alias + r")\s*[）)]", "", suffix, flags=re.I)
    # Retired assays still delimit a result segment, but never yield observations.
    # Otherwise their labelled result could overwrite a preceding active assay.
    following = [match.start() for pattern in (*ALIASES.values(), RETIRED_MARKER_PATTERN)
                 if (match := re.search(pattern, suffix, re.I)) is not None]
    if following:
        suffix = suffix[:min(following)]
    description = NON_RESULT_LABEL.search(suffix)
    if description is not None:
        suffix = suffix[:description.start()]
    result_label = RESULT_LABEL.search(suffix)
    if result_label is not None:
        suffix = suffix[result_label.end():]
    elif REFERENCE_LABEL.search(prefix) or (reference_section and not RESULT_LABEL.search(prefix)):
        return None
    reference = REFERENCE_LABEL.search(suffix)
    return suffix[:reference.start()] if reference else suffix


def _iso_date(value):
    if not isinstance(value, str):
        return None
    match = re.match(r"\s*(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", value)
    if match:
        try:
            return date(*map(int, match.groups())).isoformat()
        except ValueError:
            pass
    return None


def _text_sample_date(text):
    # A report publication date does not establish when a specimen was collected.
    dates = []
    for prefix in (r"(?:采样|采集)", r"检测"):
        dates = [_iso_date(value) for value in re.findall(prefix + r"(?:日期|时间)\s*[:：]?\s*(\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2})", text)]
        dates = [value for value in dates if value]
        if dates:
            break
    return dates[0] if len(set(dates)) == 1 else None


def bare_url(value):
    if not isinstance(value, str):
        return ""
    match = re.fullmatch(r"\[[^\]]*\]\((https?://[^\s]+)\)", value.strip())
    return match.group(1) if match else value.strip()


def extract_observations(text, *, source_id, page=1, today=None, header=None, reading_context=None):
    today = today or date.today().isoformat()
    metadata = (header + "\n" + text) if header else text
    observed_date = _text_sample_date(metadata)
    observations, issues = [], []
    context = reading_context if isinstance(reading_context, dict) else {}
    table, reference_section = context.get("table"), context.get("referenceSection", False)
    non_result_section = context.get("nonResultSection", False)
    for index, line in enumerate(text.splitlines()):
        has_marker = any(re.search(alias, line, re.I) for alias in ALIASES.values())
        # An ignored assay's reference/value row must not switch the following
        # active assays into reference-only or non-result parsing mode.
        if not has_marker and is_retired_marker_name(line):
            continue
        layout = _table_layout(line)
        if layout is not None:
            table, reference_section = layout, layout["result"] is None
            continue
        if not has_marker:
            if NON_RESULT_HEADING.fullmatch(line):
                table, non_result_section = None, True
            elif RESULT_LABEL.search(line) and not REFERENCE_LABEL.search(line) and not NON_RESULT_LABEL.search(line):
                table, reference_section, non_result_section = None, False, False
            elif REFERENCE_LABEL.search(line) and not RESULT_LABEL.search(line):
                table, reference_section = None, True
            continue
        for code, alias in ALIASES.items():
            marker = re.search(alias, line, re.I)
            if marker is None:
                continue
            segment = _result_segment(line, marker, alias, table=table,
                                      reference_section=reference_section, non_result_section=non_result_section)
            match = VALUE_UNIT.match(segment) if segment is not None else None
            if match is None:
                issues.append({"code": "AMBIGUOUS_MARKER_ROW", "marker": code,
                               "sourceId": source_id, "page": page, "line": index + 1})
                continue
            observations.append({"code": code, "value": match.group("value").replace(" ", ""),
                                 "unit": re.sub(r"\s", "", match.group("unit")), "date": observed_date,
                                 "isCurrent": observed_date == today,
                                 "source": {"sourceId": source_id, "page": page, "line": index + 1,
                                            "quote": line[:500], "identityVerified": False}})
    if observations and not observed_date:
        issues.append({"code": "SOURCE_SAMPLE_DATE_UNVERIFIED", "sourceId": source_id})
    context.update(table=table, referenceSection=reference_section, nonResultSection=non_result_section)
    return observations, issues


def _bounded_json(content):
    if len(content) > MAX_STRUCTURED_SOURCE_BYTES:
        raise ValueError("structured source exceeds limit")
    def reject_constant(_):
        raise ValueError("non-finite JSON number")
    def exact_integer(token):
        number = int(token)
        return number if -(2 ** 63) <= number < 2 ** 63 else token
    def finite_float(token):
        number = float(token)
        if not math.isfinite(number):
            raise ValueError("non-finite JSON number")
        return number
    value = json.loads(content.decode("utf-8-sig"), parse_constant=reject_constant,
                       parse_int=exact_integer, parse_float=finite_float)
    return normalize_structured_source(value)


class _PdfPages(list):
    """Keep the existing list-of-text interface with internal OCR coverage."""

    def __init__(self, values, ocr_pages):
        super().__init__(values)
        self.ocr_pages = frozenset(ocr_pages)


def _merge_observations(values, *, source_id):
    """Deduplicate values without losing native/OCR page provenance."""
    merged, normalized = {}, {}
    for row in values:
        key = (row["code"], row["value"], row["unit"], row.get("date"))
        location = {field: row["source"].get(field) for field in ("page", "line", "method")}
        if key not in merged:
            merged[key] = row
            row["source"]["locations"] = [location]
        elif location not in merged[key]["source"]["locations"]:
            merged[key]["source"]["locations"].append(location)
        raw = str(row["value"]).strip().replace(" ", "")
        match = re.fullmatch(r"([<>＜＞≤≥]?)(.+)", raw)
        try:
            numeric = str(Decimal(match[2]).normalize())
        except (DecimalException, TypeError):
            numeric = raw
        else:
            numeric = match[1].translate(str.maketrans({"＜": "<", "＞": ">"})) + numeric
        group = (row["code"], row.get("date"), row["unit"].casefold().replace(" ", ""))
        normalized.setdefault(group, set()).add(numeric)
    issues = [{"code": "SOURCE_MARKER_VALUE_CONFLICT", "sourceId": source_id, "marker": key[0]}
              for key, values in normalized.items() if len(values) > 1]
    return list(merged.values()), issues


def _walk(value, path="$", *, inherited_date=None):
    if isinstance(value, dict):
        sample_date = next((_iso_date(value.get(key)) for key in ("sampleDate", "samplingDate", "collectionDate", "testDate", "checkTime", "date") if _iso_date(value.get(key))), inherited_date)
        yield value, path, sample_date
        for key, item in value.items():
            yield from _walk(item, f"{path}.{key}", inherited_date=sample_date)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, f"{path}[{index}]", inherited_date=inherited_date)


def extract_json_observations(value, *, source_id, today=None):
    today = today or date.today().isoformat()
    observations, issues = [], []
    for row, path, sample_date in _walk(value):
        name = next((row.get(key) for key in ("code", "name", "itemName", "testItemName") if isinstance(row.get(key), str)), "")
        code = next((code for code, alias in ALIASES.items() if name == code or re.fullmatch(alias, name.strip(), re.I)), None)
        if not code:
            continue
        result = row.get("value", row.get("testResultValue"))
        unit = row.get("unit")
        if isinstance(result, bool) or not isinstance(result, (str, int, float)) or not isinstance(unit, str) or not unit.strip():
            issues.append({"code": "AMBIGUOUS_MARKER_ROW", "sourceId": source_id, "path": path, "marker": code})
            continue
        observations.append({"code": code, "value": result, "unit": unit, "date": sample_date,
                             "isCurrent": sample_date == today,
                             "source": {"sourceId": source_id, "path": path, "identityVerified": False}})
    if any(not row["date"] for row in observations):
        issues.append({"code": "SOURCE_SAMPLE_DATE_UNVERIFIED", "sourceId": source_id})
    return observations, issues


def _contains_retired_measurement(value):
    """Recognize an intentionally ignored lab row without interpreting its value."""
    if isinstance(value, (dict, list)):
        return any(
            any(is_retired_marker_name(row.get(key)) for key in
                ("code", "name", "itemName", "testItemName"))
            or any(is_retired_marker_name(key) for key in row)
            for row, _, _ in _walk(value)
        )
    if isinstance(value, str):
        for line in value.splitlines():
            cells = re.split(r"[:：|｜\t]|[<>≤≥]?\s*[+-]?\d", line.strip().lstrip("|｜ "), maxsplit=1)
            if len(cells) == 2 and is_retired_marker_name(cells[0]):
                return True
    return False


def _metric_decimal(value):
    """Bounded numeric identity, not a clinical normalization or unit conversion."""
    text = str(value).strip()
    if len(text) > 80 or not _METRIC_DECIMAL.fullmatch(text):
        return None
    try:
        number = Decimal(text)
        if (not number.is_finite() or abs(number) > Decimal("1e12")
                or not -24 <= number.as_tuple().exponent <= 12):
            return None
        return number
    except DecimalException:
        return None


def _auxiliary_metric_values_agree(code, values):
    """Recognize a bounded rounded rendering while retaining every source value.

    Compare each value to the most precise representation, never to a chain of
    coarse summaries. Equal-precision differences and count differences remain
    conflicts. Precision agreement grants neither verified identity nor use as
    a diagnosis, current measurement or product-selection input.
    """
    raw_values = list(dict.fromkeys(values))
    if len(raw_values) <= 1:
        return True
    numbers = [_metric_decimal(value) for value in raw_values]
    if any(number is None for number in numbers):
        return False
    if len(set(numbers)) == 1:
        return True
    if code not in _ROUNDABLE_AUXILIARY_METRICS:
        return False
    anchor = min(numbers, key=lambda value: (value.as_tuple().exponent, value))
    with localcontext() as context:
        context.prec = 80
        for number in numbers:
            if number == anchor:
                continue
            exponent = number.as_tuple().exponent
            if (exponent > -2 or exponent <= anchor.as_tuple().exponent
                    or not number or not anchor or number.is_signed() != anchor.is_signed()):
                return False
            quantum = Decimal(1).scaleb(exponent)
            difference = abs(anchor - number)
            # Do not guess the provider's tie-breaking convention, or allow a
            # coarse integer/one-decimal summary to conceal a meaningful change.
            if difference > _MAX_METRIC_ROUNDING_DIFFERENCE or difference >= quantum / 2:
                return False
            if anchor.quantize(quantum, rounding=ROUND_HALF_UP) != number:
                return False
    return True


def _microbiome_source(value, *, source_id, source_format):
    auxiliary = {"sourceId": source_id, "format": source_format, "status": "source_only", "origin": "testData",
                 "identityVerified": False, "sampleDate": None, "metrics": [], "enterotype": None,
                 "sourceModelIndices": [], "warnings": ["not_a_current_diagnostic_probability", "not_eligible_for_product_matching"]}
    if source_format == "json":
        recognized = False
        sample_dates = set()
        for row, path, sample_date in _walk(value):
            if sample_date:
                sample_dates.add(sample_date)
            if "microbeData" in row or "diseaseRisk" in row:
                recognized = True
            latin_name = row.get("latinName")
            if isinstance(latin_name, str) and "value" in row:
                # Do not flatten all species abundances into patient-facing health metrics.
                if ".comprehensive[" in path or ".Summary_spp[" in path:
                    auxiliary["metrics"].append({"code": latin_name, "name": METRIC_NAMES.get(latin_name, latin_name), "value": row["value"], "path": path})
                if latin_name == "Enterotype":
                    auxiliary["enterotype"] = {"raw": row["value"], "value": row["value"], "status": "source_only", "eligibleForMatching": False}
            risks = row.get("diseaseRisk")
            if isinstance(risks, dict):
                auxiliary["modelMetadata"] = {key: item for key, item in risks.items() if key in ("age", "conclusion")}
                for name, result in risks.items():
                    if name in ("age", "conclusion"):
                        continue
                    auxiliary["sourceModelIndices"].append({"name": name, "value": result, "raw": result,
                        "path": f"{path}.diseaseRisk.{name}", "interpretation": "source_model_index_not_probability"})
        if not recognized:
            return None
        auxiliary["sampleDate"] = next(iter(sample_dates)) if len(sample_dates) == 1 else None
        if len(sample_dates) > 1:
            auxiliary["warnings"].append("conflicting_source_sample_dates")
        # Preserve the source object for traceability; it is never used as instructions.
        auxiliary["rawData"] = value
        return auxiliary
    if not re.search(r"肠菌|菌群|肠型|多样性|菌种|微生态", value):
        return None
    auxiliary.update(sampleDate=_text_sample_date(value), summaryText=value[:500000])
    patterns = {"GMWI2": r"(?:肠菌健康指数|GMWI2)\s*(?:为|是|[:：])?\s*([+-]?\d+(?:\.\d+)?)",
                "Species_Num": r"菌种数\s*(?:为|是|[:：])?\s*(\d+)",
                "Diversity_Index": r"多样性指数\s*(?:为|是|[:：])?\s*([+-]?\d+(?:\.\d+)?)"}
    for code, pattern in patterns.items():
        for match in re.finditer(pattern, value, re.I):
            auxiliary["metrics"].append({"code": code, "name": METRIC_NAMES[code], "value": match.group(1), "raw": match.group(0)})
    return auxiliary


class GutBarrierSources:
    def __init__(self, *, downloader=None, parsing_client=None, timeout_seconds=240, concurrency=2):
        self.downloader = downloader or HttpDocumentDownloader(timeout_seconds=35, max_bytes=25 * 1024 * 1024)
        self.parsing_client = parsing_client
        self.timeout_seconds = timeout_seconds
        self._semaphore = asyncio.Semaphore(concurrency)
        # Cancelling asyncio.to_thread only stops its waiter, not a running SDK
        # call. This separate guard follows the real synchronous call lifetime.
        self._ocr_slots = threading.BoundedSemaphore(concurrency)

    async def collect(self, request, *, evaluation_date=None):
        entries = []
        for item in request.file_list:
            if item.url:
                entries.append((item.url, item.file_type or "pdf", "fileList"))
        for item in request.test_data:
            for key in ("resultDetailUrl", "resultSummaryUrl"):
                value = item.get(key)
                if value:
                    entries.append((bare_url(value), "auto", key))
        seen, unique = set(), []
        for entry in entries:
            if entry[0] not in seen:
                seen.add(entry[0])
                unique.append(entry)
        results = await asyncio.gather(*(self._one(*entry, evaluation_date=evaluation_date) for entry in unique))
        if request.microbiome_data:
            # Inline source data follows the same bounded provenance parsing
            # as downloaded JSON. It cannot select a risk model or a product.
            summary = {"sourceId": "request_microbiomeData", "location": "microbiomeData",
                       "fileType": "json", "status": "unavailable", "observationCount": 0}
            try:
                content = json.dumps(request.microbiome_data, ensure_ascii=False, allow_nan=False).encode("utf-8")
                inline = self._structured_or_text(content, summary=summary, evaluation_date=evaluation_date)
                for auxiliary in inline.get("auxiliarySources", []):
                    auxiliary["origin"] = "microbiomeData"
                results.append(inline)
            except Exception as error:
                results.append({"observations": [], "summary": summary,
                                "issues": [{"code": "SOURCE_UNAVAILABLE", "sourceId": summary["sourceId"],
                                            "errorType": type(error).__name__}]})
        auxiliary = [value for result in results for value in result.get("auxiliarySources", [])]
        issues = [value for result in results for value in result["issues"]]
        metrics = {}
        for source in auxiliary:
            for metric in source["metrics"]:
                metrics.setdefault(metric["code"], []).append((source["sourceId"], str(metric["value"])))
        for code, values in metrics.items():
            if not _auxiliary_metric_values_agree(code, [value for _, value in values]):
                issues.append({"code": "AUXILIARY_METRIC_CONFLICT", "metric": code,
                               "sourceIds": list(dict.fromkeys(source for source, _ in values)),
                               "values": list(dict.fromkeys(value for _, value in values))})
                for source in auxiliary:
                    if source["sourceId"] in {item[0] for item in values}:
                        source["warnings"].append("conflicting_source_metric:" + code)
        return {"observations": [value for result in results for value in result["observations"]],
                "sources": [result["summary"] for result in results],
                "issues": issues, "context": {"microbiomeSources": auxiliary} if auxiliary else {}}

    async def _one(self, url, file_type, location, *, evaluation_date=None):
        source_id = "source_" + hashlib.sha256(url.encode()).hexdigest()[:20]
        summary = {"sourceId": source_id, "location": location, "fileType": file_type,
                   "status": "unavailable", "observationCount": 0}
        try:
            async with self._semaphore:
                deadline = asyncio.get_running_loop().time() + self.timeout_seconds
                async with asyncio.timeout(self.timeout_seconds):
                    content = await self.downloader.download(url)
                    if len(content) > MAX_DOCUMENT_BYTES:
                        raise ValueError("document exceeds byte limit")
                    summary["sha256"] = hashlib.sha256(content).hexdigest()
                    is_pdf = content.startswith(b"%PDF-")
                    is_image = content.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n"))
                    if not is_pdf and not is_image:
                        if location == "fileList":
                            raise ValueError("declared file did not return a supported document")
                        return self._structured_or_text(content, summary=summary, evaluation_date=evaluation_date)
                    summary["format"] = "pdf" if is_pdf else "image"
                    pages = await asyncio.to_thread(self._pdf_pages, content) if is_pdf else [""]
                # A failed page must not discard text already read from another
                # page. OCR calls use the remaining overall source time budget.
                return await self._document_source(content, pages, url=url, summary=summary,
                    is_pdf=is_pdf, deadline=deadline, evaluation_date=evaluation_date)
        except _PdfPageLimitExceeded as error:
            return self._page_limited_source(error, summary=summary)
        except Exception as error:
            return {"observations": [], "summary": summary,
                    "issues": [{"code": "SOURCE_UNAVAILABLE", "sourceId": source_id,
                                "errorType": type(error).__name__}]}

    @staticmethod
    def _page_limited_source(error, *, summary):
        """Retain bounded document-type metadata without bypassing the page cap."""
        text = "\n".join(error.preview_pages)
        source_id = summary["sourceId"]
        issues = [{"code": "SOURCE_PAGE_LIMIT_EXCEEDED", "sourceId": source_id,
                   "pageCount": error.page_count, "pageLimit": error.page_limit,
                   "reason": "document_page_count_exceeds_limit"}]
        for page, error_type in error.preview_errors:
            issues.append({"code": "SOURCE_PREVIEW_UNAVAILABLE", "sourceId": source_id,
                           "page": page, "errorType": error_type})
        document_type = ("microbiome_report" if re.search(r"肠菌|菌群|肠型|宏基因组|微生态|\bNGS\b|microbiom", text, re.I)
                         else "health_examination_report" if re.search(r"体检|健康检查|health\s+(?:check|examin)", text, re.I)
                         else "unrecognized")
        summary.update(status="page_limit_exceeded",
                       reason="document_page_count_exceeds_limit",
                       pageCount=error.page_count, pageLimit=error.page_limit,
                       observationCount=0, auxiliaryCount=0, previewOnly=True,
                       previewPageCount=len(error.preview_pages), documentType=document_type)
        return {"observations": [], "summary": summary, "issues": issues, "auxiliarySources": []}

    async def _document_source(self, content, pages, *, url, summary, is_pdf, deadline, evaluation_date):
        if (not pages or len(pages) > MAX_DOCUMENT_PAGES
                or not all(isinstance(text, str) for text in pages)
                or sum(map(len, pages)) > MAX_DOCUMENT_TEXT):
            raise ValueError("invalid or oversized document text")
        source_id = summary["sourceId"]
        required = set(getattr(pages, "ocr_pages", (index for index, text in enumerate(pages) if not text.strip())))
        entries = [(index + 1, "native_text", text) for index, text in enumerate(pages) if text.strip()]
        page_audit = [{"page": index + 1, "nativeChars": len(text), "ocrRequired": index in required,
                       "ocrStatus": "not_required", "ocrChars": 0} for index, text in enumerate(pages)]
        issues, text_size, attempted = [], sum(map(len, pages)), 0
        # Empty pages take precedence over image regions on already-readable pages.
        targets = sorted(required, key=lambda index: (bool(pages[index].strip()), index))
        for index in targets:
            audit = page_audit[index]
            audit["ocrStatus"] = "unavailable"
            issue = {"sourceId": source_id, "page": index + 1, "stage": "ocr"}
            remaining = deadline - asyncio.get_running_loop().time()
            if self.parsing_client is None:
                issues.append({**issue, "code": "OCR_UNAVAILABLE"})
                continue
            if attempted >= MAX_OCR_PAGES:
                issues.append({**issue, "code": "OCR_PAGE_LIMIT_REACHED"})
                continue
            if remaining <= 0:
                issues.append({**issue, "code": "OCR_BUDGET_EXHAUSTED"})
                continue
            attempted += 1
            try:
                async with asyncio.timeout(min(OCR_PAGE_TIMEOUT_SECONDS, remaining)):
                    page_content = await asyncio.to_thread(self._pdf_page_bytes, content, index) if is_pdf else content
                    extension = "pdf" if is_pdf else self._image_extension(content)
                    result = await asyncio.to_thread(self._parse_page, page_content, url=url,
                        filename=f"document-page-{index + 1}.{extension}", extension=extension)
                    text = parsed_document_text(result)
                    if text_size + len(text) > MAX_DOCUMENT_TEXT:
                        raise ValueError("document OCR text exceeds limit")
                    text_size += len(text)
                    entries.append((index + 1, "ocr", text))
                    audit.update(ocrStatus="parsed", ocrChars=len(text))
            except _OcrCapacityUnavailable:
                issues.append({**issue, "code": "OCR_CAPACITY_EXHAUSTED"})
            except Exception as error:
                audit["ocrStatus"] = "failed"
                issues.append({**issue, "code": "SOURCE_UNAVAILABLE", "errorType": type(error).__name__})
        entries.sort(key=lambda entry: (entry[0], entry[1] != "native_text"))
        text = "\n".join(entry[2] for entry in entries)
        # Carry sample dates across retained pages. Conflicting dates remain
        # unresolved; source names do not determine clinical-data inclusion.
        header = "\n".join(line for line in text.splitlines()
                           if re.search(r"采样|采集|检测(?:日期|时间)", line))
        observations, reading_context, page_context, last_page = [], {}, {}, None
        for page, method, page_text in entries:
            if page != last_page:
                page_context, last_page = dict(reading_context), page
            # Native and OCR views of the same page share the preceding page's
            # context, not the other view's end-of-page table/reference state.
            reading_context = dict(page_context)
            values, errors = extract_observations(page_text, header=header, source_id=source_id,
                page=page, today=evaluation_date, reading_context=reading_context)
            for row in values:
                row["source"]["method"] = method
            observations.extend(values)
            # Keep one issue per identical document-wide finding.
            for error in errors:
                if error not in issues:
                    issues.append(error)
        observations, conflicts = _merge_observations(observations, source_id=source_id)
        issues.extend(conflicts)
        auxiliary = _microbiome_source(
            text, source_id=source_id, source_format="text")
        if auxiliary is not None:
            auxiliary.update(origin=summary["location"], format=summary["format"])
        failed_pages = [row["page"] for row in page_audit if row["ocrRequired"] and row["ocrStatus"] != "parsed"]
        if not observations and auxiliary is None and not issues and not _contains_retired_measurement(text):
            issues.append({"code": "UNRECOGNIZED_SOURCE_CONTENT", "sourceId": source_id})
        status = "partially_parsed" if failed_pages and entries else "needs_ocr" if failed_pages else "parsed"
        summary.update(status=status, observationCount=len(observations), pageCount=len(pages),
                       auxiliaryCount=int(auxiliary is not None), pageAudit=page_audit, failedPages=failed_pages)
        return {"observations": observations, "summary": summary, "issues": issues,
                "auxiliarySources": [auxiliary] if auxiliary else []}

    def _parse_page(self, content, *, url, filename, extension):
        # Do not queue SDK work behind a timed-out call. Acquire in the worker
        # itself and retain ownership until submit AND polling really finish.
        if not self._ocr_slots.acquire(blocking=False):
            raise _OcrCapacityUnavailable()
        try:
            # Preparation occurs only after the worker starts and gets a slot;
            # cancellation while to_thread is queued cannot leak a temp file.
            prepared = prepare_document(self.parsing_client, file_url=url,
                filename=filename, file_type=extension, content=content)
            task_id = prepared.submit()  # submit owns cleanup, including errors.
            if not isinstance(task_id, str) or not task_id.strip():
                raise ValueError("invalid OCR task")
            return self.parsing_client.get_result(task_id, poll_interval=1)
        finally:
            self._ocr_slots.release()

    @staticmethod
    def _structured_or_text(content, *, summary, evaluation_date=None):
        source_id = summary["sourceId"]
        if len(content) > MAX_STRUCTURED_SOURCE_BYTES:
            raise ValueError("text source exceeds limit")
        text = content.decode("utf-8-sig").strip()
        if not text or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text):
            raise ValueError("invalid source text")
        if text.startswith(("{", "[")):
            value = _bounded_json(content)
            observations, issues = extract_json_observations(value, source_id=source_id, today=evaluation_date)
            source_format = "json"
        else:
            if len(text) > MAX_DOCUMENT_TEXT:
                raise ValueError("source text exceeds limit")
            # HTML login/error pages and XML are never clinical plaintext.
            if re.search(r"<!doctype|<html\b|<body\b|<script\b|<\?xml", text, re.I):
                raise ValueError("source returned markup rather than a report")
            value, source_format = text, "text"
            observations, issues = extract_observations(text, source_id=source_id, today=evaluation_date)
        auxiliary = _microbiome_source(value, source_id=source_id, source_format=source_format)
        if not observations and auxiliary is None and not issues and not _contains_retired_measurement(value):
            issues.append({"code": "UNRECOGNIZED_SOURCE_CONTENT", "sourceId": source_id})
        summary = {**summary, "status": "parsed", "format": source_format,
                   "observationCount": len(observations), "auxiliaryCount": int(auxiliary is not None)}
        return {"observations": observations, "summary": summary, "issues": issues,
                "auxiliarySources": [auxiliary] if auxiliary else []}

    @staticmethod
    def _pdf_pages(content):
        import pymupdf
        with pymupdf.open(stream=content, filetype="pdf") as document:
            if document.is_encrypted or not len(document):
                raise ValueError("encrypted or empty PDF")
            if len(document) > MAX_DOCUMENT_PAGES:
                preview, errors, remaining = [], [], MAX_DOCUMENT_PREVIEW_TEXT
                for index in range(min(len(document), MAX_DOCUMENT_PREVIEW_PAGES)):
                    if remaining <= 0:
                        break
                    try:
                        text = document[index].get_text(sort=True)[:remaining]
                    except Exception as error:
                        text = ""
                        errors.append((index + 1, type(error).__name__))
                    preview.append(text)
                    remaining -= len(text)
                raise _PdfPageLimitExceeded(page_count=len(document), page_limit=MAX_DOCUMENT_PAGES,
                                           preview_pages=preview, preview_errors=errors)
            pages, needs_ocr = [], set()
            for index, page in enumerate(document):
                text = page.get_text(sort=True)
                pages.append(text)
                # A text header/watermark does not mean the scanned result
                # image below it is readable. Large raster regions need OCR.
                area = max(1, page.rect.get_area())
                images = page.get_image_info()
                large_image = len(images) > 100 or any(
                    (pymupdf.Rect(image["bbox"]) & page.rect).get_area() / area >= 0.15
                    for image in images[:100])
                # A genuinely blank PDF page has no text or content stream;
                # it is not a failed scan and must not consume an OCR slot.
                if (not text.strip() and bool(page.get_contents())) or large_image:
                    needs_ocr.add(index)
            if sum(map(len, pages)) > MAX_DOCUMENT_TEXT:
                raise ValueError("PDF text exceeds limit")
            return _PdfPages(pages, needs_ocr)

    @staticmethod
    def _pdf_page_bytes(content, index):
        """Submit exactly one bounded original page, keeping source page numbers."""
        import pymupdf
        with pymupdf.open(stream=content, filetype="pdf") as document:
            if document.is_encrypted or len(document) > MAX_DOCUMENT_PAGES or not 0 <= index < len(document):
                raise ValueError("invalid PDF page")
            with pymupdf.open() as extracted:
                extracted.insert_pdf(document, from_page=index, to_page=index)
                content = extracted.tobytes(garbage=3, deflate=True)
        if len(content) > MAX_DOCUMENT_BYTES:
            raise ValueError("PDF page exceeds limit")
        return content

    @staticmethod
    def _image_extension(content):
        if content.startswith(b"\xff\xd8\xff"):
            return "jpg"
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        raise ValueError("unsupported file signature")


class GutBarrierProducts:
    def __init__(self, url, *, downloader=None):
        self.url = url
        self.downloader = downloader or HttpDocumentDownloader(timeout_seconds=15, max_bytes=2 * 1024 * 1024)

    async def fetch(self):
        if not self.url:
            return None
        url = validate_report_endpoint(self.url, setting="REPORT_PRODUCT_URL")
        content = await self.downloader.download(url)
        value = json.loads(content)
        if not isinstance(value, dict):
            raise ValueError("product catalog must be an object")
        return value
