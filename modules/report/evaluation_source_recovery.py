"""Evaluate-only categorical summaries from an already verified source table.

This helper does not open files, fetch documents or reinterpret taxa. Callers
provide the ownership-checked original document and its actual table pages.
The resulting metadata stays in the existing excluded evaluate-summary lane.
"""
from __future__ import annotations

import re
import unicodedata

from pydantic import Field

from modules.report.schemas import SourceMetric


def _objective_clinical_tables(document):
    """Keep native columns and complete geometric rows under their own header."""
    from modules.report import parsing

    def compact(value):
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or "")))

    native = [table for table in document.tables if table.native_clinical_columns]
    yield from native
    for table in document.tables:
        if table.native_clinical_columns:
            continue
        header, groups = parsing._clinical_table_role_groups(table.rows)
        section_lines = [line.strip() for line in str(table.context or "").splitlines()
            if re.match(r"^\d{1,3}[.、．]\s*[^\d\s]", line.strip())]
        section = section_lines[-1] if section_lines else None
        if header is None:
            # A geometric single-row table often omits its header. Its own
            # preceding context must contain one complete literal header.
            # Do not infer a column order from an unrelated native table.
            context = str(table.context or "")
            if section:
                context = context[context.rfind(section) + len(section):]
            candidates = []
            for line in context.splitlines():
                cells = line.strip().split()
                _, roles = parsing._clinical_table_role_groups([cells])
                if (len(roles) == 1 and {"name", "value", "reference", "unit", "status"} <= roles[0].keys()
                        and len(cells) == len(roles[0])):
                    candidates.append(cells)
            unique = {tuple(cells) for cells in candidates}
            if len(unique) != 1:
                continue
            columns = list(next(iter(unique)))
            if any(len(row) != len(columns) for row in table.rows):
                continue
            rows = [columns, *table.rows]
        else:
            if len(groups) != 1 or not {"name", "value", "reference"} <= groups[0].keys():
                continue
            rows = table.rows
            # An in-table numbered heading establishes this table's section.
            own_sections = [str(row[0]).strip() for row in rows[:header]
                if row and re.match(r"^\d{1,3}[.、．]\s*[^\d\s]", str(row[0]).strip())]
            if own_sections:
                section = own_sections[-1]
        scoped = [candidate for candidate in native
            if candidate.page_number == table.page_number and section
            and compact(candidate.section_title) == compact(section)]
        dates = {candidate.observed_at for candidate in scoped if candidate.observed_at}
        # This only restores an already scoped examination date; a document
        # header or another examination cannot date an undated result.
        observed_at = next(iter(dates)) if len(dates) == 1 else None
        yield parsing.ExtractedTable(table.page_number, rows, table.context,
            observed_at, section, True)


def evaluation_clinical_source_rows(document, *, filename, checksum, report_type):
    """Recover objective cells from this exact clinical original for evaluate.

    A partial shared interpretation can omit a result even when its complete
    positioned row survives. Native geometry and an explicit result/reference
    header establish ownership; model candidates and unresolved prose do not.
    Dates, units, flags and sections are copied only when printed in that row's
    existing native scope. Selection by specimen/time remains a later step.
    """
    from modules.report import parsing

    if (report_type not in {"checkup", "imaging", "lab"} or not filename or not checksum
            or document.filename != filename
            or document.metadata.get("content_sha256") != checksum):
        return ()
    rows = []
    seen = set()
    for table in _objective_clinical_tables(document):
        if (not table.native_clinical_columns or type(table.page_number) is not int
                or table.page_number < 1):
            continue
        original = parsing.ExtractedDocument(document.filename, document.source_type, "", [table])
        for row in parsing.original_clinical_table_rows(original):
            if str(row.reference_range or "").strip() in {"", "-", "--", "/", "—"}:
                continue
            # An objective numeric or short categorical result is recoverable;
            # a clinical opinion/education paragraph is not a measurement cell.
            value = str(row.test_result_value or "").strip()
            scalar = re.fullmatch(r"[<>≤≥=]?\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?(?:\+)?|[-+]{1,4}", value)
            categorical = value in {"阴性", "阳性", "正常", "异常", "未查见", "未见", "未检出", "检出"}
            if not scalar and not categorical:
                continue
            row = row.model_copy(deep=True)
            row.__pydantic_extra__["sourceChecksum"] = checksum
            row.__pydantic_extra__["sourceReportType"] = report_type
            identity = row.model_dump_json(by_alias=True)
            if identity not in seen:
                seen.add(identity)
                rows.append(row)
    return tuple(rows)


HEALTH_SUMMARY_CODES = {
    "health_summary_beneficial": "有益菌",
    "health_summary_scfa_producers": "产短链脂肪酸的菌",
    "health_summary_pathogenic": "致病菌",
    "health_summary_opportunistic": "条件致病菌",
    "health_summary_pathogenic_fungi": "致病性真菌",
    "health_summary_pathogenic_viruses": "致病性病毒",
    "health_summary_pathogenic_parasites": "致病性寄生虫",
    "health_summary_virulence_genes": "毒力基因",
    "health_summary_antibiotic_resistance_genes": "抗生素耐药基因",
    "health_summary_anti_inflammatory": "肠道抗炎能力",
    "health_summary_gut_immunity": "肠道免疫力",
}


class EvaluationHealthSummaryMetric(SourceMetric):
    """Private evaluate provenance without extending shared SourceMetric JSON."""
    source_page: int = Field(alias="sourcePage", exclude=True)
    source_file: str = Field(alias="sourceFile", exclude=True)
    source_checksum: str = Field(alias="sourceChecksum", exclude=True)
    observed_at: str | None = Field(default=None, alias="observedAt", exclude=True)
    source_section: str | None = Field(default=None, alias="sourceSection", exclude=True)
    source_text: str = Field(alias="sourceText", exclude=True)


def _label(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or "")))


def evaluation_health_summary_metrics(document):
    """Recover literal patient result cells, never a count or an inferred grade."""
    checksum = document.metadata.get("content_sha256")
    if not document.filename or not isinstance(checksum, str) or not checksum:
        return []
    codes = {name: code for code, name in HEALTH_SUMMARY_CODES.items()}
    categorical = {"正常", "异常", "检出", "未检出", "偏高", "偏低", "缺乏", "超标"}
    recovered = []
    seen = set()
    for table in document.tables:
        if type(table.page_number) is not int or table.page_number < 1 or not table.rows:
            continue
        header = [_label(cell) for cell in table.rows[0]]
        names = [i for i, cell in enumerate(header) if cell in {"检测项目", "评估项目", "检验项目", "项目名称"}]
        results = [i for i, cell in enumerate(header) if cell in {"检测结果", "评估结果", "结果判定", "本次结果"}]
        if len(names) != 1 or len(results) != 1:
            continue
        for cells in table.rows[1:]:
            if max(names[0], results[0]) >= len(cells):
                continue
            name = _label(cells[names[0]])
            value = str(cells[results[0]]).strip()
            if name not in codes or _label(value) not in categorical:
                continue
            identity = (codes[name], value, table.page_number)
            if identity in seen:
                continue
            seen.add(identity)
            # A categorical result such as 未检出 is itself the printed source
            # status. Copy it literally; it is not a newly computed risk grade.
            recovered.append(EvaluationHealthSummaryMetric(code=codes[name], name=name, value=value,
                status=value, sourcePage=table.page_number, sourceFile=document.filename,
                sourceChecksum=checksum, observedAt=table.observed_at,
                sourceSection=table.section_title, sourceText=" | ".join(map(str, cells))))
    return recovered
