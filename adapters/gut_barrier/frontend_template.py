"""Version-pinned frontend copy for local previews of dynamic callback reports.

This is presentation composition only. It never evaluates measurements, selects
interventions or modifies the callback. The JSON asset is the frontend hand-off;
runtime expansion reads that immutable snapshot, not today's clinical catalog.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from modules.gut_barrier.display_versions import (
    CLOSED_BOUNDARY_DISPLAY_TEMPLATE, LATEST_DISPLAY_TEMPLATE, display_capabilities, resolve_display_template,
)


DYNAMIC_SCHEMA_VERSION = "gut-barrier-public.v2"
STATIC_TEMPLATE_VERSION = "gut-barrier-static.v1"
STATIC_TEMPLATE_DIRECTORY = Path(__file__).resolve().parent / "assets"
# The legacy builder/default remains version-pinned; only the current release
# is bundled. Historical preview copies must be supplied from an external archive.
BUNDLED_STATIC_TEMPLATE_VERSION = LATEST_DISPLAY_TEMPLATE
BUNDLED_STATIC_TEMPLATE_PATH = STATIC_TEMPLATE_DIRECTORY / f"{BUNDLED_STATIC_TEMPLATE_VERSION}.json"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ATTENTION_INTRODUCTION = {
    "kind": "paragraph", "title": None,
    "text": "本章结合本次可用资料，列出有依据的健康关注方向。健康关注指数是自定义、尚未经过临床验证的0–10分排序工具，"
            "用于安排资料核实与健康评估的先后，不是患病概率，不能用于诊断、治疗决策或临床紧急分诊。"
            "未列入的疾病不代表低风险或已排除；如另有已注册模型结果，应按该模型说明单独解释。",
}
_ATTENTION_SCALE_TEXT = (
    "本报告的健康关注指数采用自定义0–10分标尺，目前尚未经过临床验证，仅用于本次资料支持的关注排序。"
    "1–3分为基础关注，4–6分为加强关注，7–10分为优先评估；这些名称不表示疾病风险高低或临床紧急程度。"
    "分值不能解读为百分比或患病概率，不能用于诊断和治疗决策。空值表示未提供相应数值，未列入不表示无风险。"
    "IBFC用于肠道屏障评分，与健康关注指数不是同一标尺，不相互换算。"
    "若表内另列已注册模型数值（包括0分），应依其模型说明解释，不与自定义关注指数合并排序或直接比较；"
    "健康关注图仅展示自定义关注项目。"
)


def uses_attention_index(sections) -> bool:
    """Only the explicit new-report callout opts into attention-only copy."""
    if not isinstance(sections, (list, tuple)):
        return False
    return any(
        isinstance(section, dict) and section.get("id") == "disease_risks"
        and section.get("visible") is not False
        and isinstance(section.get("blocks"), (list, tuple))
        and any(isinstance(block, dict) and block.get("kind") == "callout"
                and block.get("title") == "本次关注指数" for block in section["blocks"])
        for section in sections)


def _fill_quality_control(sections, source_overview, *, canonical_dynamic_layout=False):
    """Bind only public patient facts; normal results never imply passed QC.

    This fills only the fallback template, never a callback-owned QC table.
    Unprovided laboratory QC and signatures deliberately remain null.
    Duplicate/conflicting facts are not resolved by first/last-wins selection.
    """
    if source_overview.get("visible") is False:
        return
    facts = {}
    for block in source_overview.get("blocks", []):
        if block.get("kind") == "facts" and block.get("title") == "受检者信息":
            for item in block.get("items", []):
                facts.setdefault(item.get("label"), []).append(item.get("value"))
    bindings = ({"姓名": "姓名", "性别": "性别", "年龄": "年龄"}
                if canonical_dynamic_layout else
                {"受检者姓名": "姓名", "性别": "性别", "年龄": "年龄",
                 "样本类型": "样本类型", "检测项目": "检测项目"})
    title = "4.1 质控校验" if canonical_dynamic_layout else "5.1 质控校验"
    for block in (sections.get("appendix") or {}).get("blocks", []):
        if block.get("kind") != "table" or block.get("title") != title:
            continue
        for row in block.get("rows", []):
            for index in ((0, 2) if canonical_dynamic_layout else (0,)):
                values = facts.get(bindings.get(row[index]), [])
                if len(values) == 1 and isinstance(values[0], (str, int, float, bool)):
                    row[index + 1] = values[0]


def _archived_template_path(template_version: str, archive_directory: str | Path | None) -> Path:
    if not archive_directory:
        raise FileNotFoundError(
            f"historical gut barrier template {template_version} is archived; supply "
            "archive_directory or --template-archive-dir explicitly with its directory outside the project")
    directory = Path(archive_directory).expanduser()
    if not directory.is_absolute():
        raise ValueError("gut barrier template archive directory must be an absolute path outside the project")
    resolved = directory.resolve()
    if any(path == _PROJECT_ROOT or _PROJECT_ROOT in path.parents for path in (directory, resolved)):
        raise ValueError("gut barrier template archive must be outside the project directory")
    path = (resolved / f"{template_version}.json").resolve()
    if resolved not in path.parents or path == _PROJECT_ROOT or _PROJECT_ROOT in path.parents:
        raise ValueError("archived gut barrier template must stay inside its external archive directory")
    if not path.is_file():
        raise FileNotFoundError(f"archived gut barrier template {template_version} not found: {path}")
    return path


def load_static_template(template_version: str = STATIC_TEMPLATE_VERSION, *,
                         archive_directory: str | Path | None = None) -> dict:
    """Read the exact v2-bound asset; no fallback to a newer content catalog."""
    template_version = resolve_display_template({}, template_version)
    path = BUNDLED_STATIC_TEMPLATE_PATH if template_version == BUNDLED_STATIC_TEMPLATE_VERSION else (
        STATIC_TEMPLATE_DIRECTORY / f"{template_version}.json" if template_version == CLOSED_BOUNDARY_DISPLAY_TEMPLATE else
        _archived_template_path(template_version, archive_directory))
    asset = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(asset, dict) or asset.get("templateVersion") != template_version
            or asset.get("reportSchemaVersion") != DYNAMIC_SCHEMA_VERSION):
        raise ValueError("gut barrier frontend template version mismatch")
    return asset


def _reference_keys(item):
    if not isinstance(item, dict):
        return set()
    keys = {(key, str(item[key]).strip()) for key in ("url", "doi") if item.get(key)}
    return keys or ({("citation", str(item["citation"]).strip())} if item.get("citation") else set())


def _publication_alias(item):
    """Known duplicate editions only; similar titles are not enough to merge."""
    if not isinstance(item, dict) or not isinstance(item.get("url"), str):
        return None
    try:
        parsed = urlsplit(item["url"])
    except ValueError:
        return None
    if (parsed.hostname, parsed.path.rstrip("/")) in {
        ("www.who.int", "/publications/i/item/9789240015128"),
        ("iris.who.int", "/bitstream/handle/10665/336656/9789240015128-eng.pdf"),
    }:
        return "who-physical-activity-2020-9789240015128"
    return None


def _merge_references(fixed_blocks, dynamic_blocks, *, reference_aliases=None):
    """Preserve static numeric/补 numeric positions and append unique sources."""
    output = deepcopy(fixed_blocks)
    groups = [block for block in output if block.get("kind") == "references"]
    supplemental = next(block for block in groups if block.get("title") == "营养与微生态干预补充依据")
    known = set()
    for group in groups:
        for item in group.get("items", []):
            known.update(_reference_keys(item))
    for block in dynamic_blocks:
        if not isinstance(block, dict) or block.get("kind") != "references":
            # Unknown dynamic blocks are not discarded by preview composition.
            if block not in output:
                output.append(deepcopy(block))
            continue
        for item in block.get("items", []):
            keys = _reference_keys(item)
            # Named case citations carry their own stable reference identity.
            # An older static entry for the same URL must not swallow that ID.
            case_citation = str(item.get("citation") or "").startswith("[评估")
            if keys and known.intersection(keys) and not case_citation:
                continue
            if case_citation and any(existing.get("citation") == item.get("citation") for existing in supplemental["items"]):
                continue
            supplemental["items"].append(deepcopy(item))
            known.update(keys)
    if reference_aliases is not None:
        # The original merge above establishes the historical citation order.
        # Keep all fixed positions; only coalesce known duplicate publications
        # among appended entries and record every numeric remapping for HTML.
        fixed_count = next(len(block.get("items", [])) for block in fixed_blocks
                           if block.get("kind") == "references"
                           and block.get("title") == "营养与微生态干预补充依据")
        aliases, publications, unique = {}, {}, []
        for old_index, item in enumerate(supplemental["items"], 1):
            publication = _publication_alias(item)
            named_case = str(item.get("citation") or "").startswith("[评估")
            if old_index > fixed_count and publication in publications and not named_case:
                aliases[old_index] = publications[publication]
                continue
            unique.append(item)
            aliases[old_index] = len(unique)
            if publication:
                publications.setdefault(publication, len(unique))
        supplemental["items"] = unique
        reference_aliases["supplement-1"] = aliases
    return output


def _patient_first_static_copy(asset, sections):
    """Keep v9 callback numbers stable and append independently numbered copy.

    Only server-owned fixed text is rewritten. The callback remains the primary
    citation namespace; matching fixed publications reuse its URL/DOI identity.
    Historical templates retain their former merge.
    """
    asset = deepcopy(asset)
    groups = [block for block in asset["appendixBlocks"] if block.get("kind") == "references"]
    primary = next((block for block in groups if "参考文献" in (block.get("title") or "")), groups[0])
    groups = [primary, *[block for block in groups if block is not primary]]
    appendix = next((section for section in sections if section.get("id") == "appendix"), {})
    dynamic_references = [deepcopy(item) for block in appendix.get("blocks", [])
                          if block.get("kind") == "references" for item in block.get("items", [])]
    references = dynamic_references[:]
    known = [_reference_keys(item) for item in references]
    primary_indices, supplemental_indices = [], []
    for block in groups:
        indices = primary_indices if block is primary else supplemental_indices
        for item in block.get("items", []):
            keys = _reference_keys(item)
            existing = next((index for index, candidate in enumerate(references, 1)
                             if keys & known[index - 1] or not keys and candidate == item), None)
            if existing is None:
                references.append(deepcopy(item))
                known.append(keys)
                existing = len(references)
            indices.append(existing)
    pattern = re.compile(r"\[(?P<group>补充|补)?(?P<numbers>\d+(?:\s*[-–—,，、]\s*\d+)*)\]")

    def replace(match):
        mapping = supplemental_indices if match.group("group") else primary_indices
        limit = len(mapping)
        indices = []
        for part in re.split(r"[,，、]", match.group("numbers")):
            bounds = re.split(r"[-–—]", part)
            if len(bounds) > 2 or any(len(value.strip()) > 5 for value in bounds):
                return match.group()
            start, end = int(bounds[0]), int(bounds[-1])
            if start < 1 or end < start or end > limit or end - start > 100:
                return match.group()
            indices.extend(mapping[number - 1] for number in range(start, end + 1))
        return "[" + ",".join(map(str, dict.fromkeys(indices))) + "]"

    def rewrite(value):
        if isinstance(value, str):
            return pattern.sub(replace, value)
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if isinstance(value, dict):
            return {key: rewrite(item) for key, item in value.items()}
        return value

    for key in ("overviewInsertions", "fixedSections", "diseaseIntroduction", "diseaseMechanisms"):
        asset[key] = rewrite(asset[key])
    blocks = []
    for block in asset["appendixBlocks"]:
        if block.get("kind") != "references":
            blocks.append(rewrite(block))
        elif block is primary:
            blocks.append({**block, "items": references})
    asset["appendixBlocks"] = blocks
    return asset


def _merge_patient_first_references(fixed_blocks, dynamic_blocks):
    """The prepared primary bibliography already contains every dynamic row."""
    output = deepcopy(fixed_blocks)
    for block in dynamic_blocks:
        if block.get("kind") != "references" and block not in output:
            output.append(deepcopy(block))
    return output


def _fill_overview(section, asset, *, canonical_dynamic_layout=False):
    blocks = section.get("blocks", [])
    for insertion in asset["overviewInsertions"]:
        fixed = insertion["block"]
        if (fixed in blocks if canonical_dynamic_layout and fixed.get("title") is None else
                any(block.get("kind") == fixed["kind"] and block.get("title") == fixed["title"] for block in blocks)):
            continue
        anchor = next((index for index, block in enumerate(blocks)
                       if block.get("kind") == insertion["anchor"]["kind"]
                       and block.get("title") == insertion["anchor"]["title"]), None)
        if anchor is not None:
            blocks.insert(anchor + (insertion["position"] == "after"), deepcopy(fixed))
    for slot in asset["overviewSlots"]:
        for block in blocks:
            if block.get("kind") != slot["kind"] or block.get("title") != slot["title"]:
                continue
            for item in block.get("items", []):
                if item.get("label") == slot["itemLabel"] and item.get(slot["field"]) is None:
                    item[slot["field"]] = deepcopy(slot["value"])


def _fill_diseases(section, asset, *, clean_review_layout=False):
    blocks = section.get("blocks", [])
    attention_mode = uses_attention_index([section])
    intro = _ATTENTION_INTRODUCTION if attention_mode else asset["diseaseIntroduction"]
    if clean_review_layout:
        # The current patient-owned index notice is sufficient. Remove only
        # exact known injected copy; unknown clinical paragraphs stay intact.
        blocks[:] = [block for block in blocks
                     if block not in (asset["diseaseIntroduction"], _ATTENTION_INTRODUCTION)]
    elif attention_mode:
        blocks[:] = [block for block in blocks if block != asset["diseaseIntroduction"]]
    if not clean_review_layout and not any(block == intro for block in blocks):
        blocks.insert(0, deepcopy(intro))
    if attention_mode:
        # A missing dynamic basis is unknown, not permission to backfill a
        # generic gut-axis mechanism from the historical fixed disease list.
        return
    mechanisms = {(item["tableTitle"], item["diseaseName"]): item["mechanism"]
                  for item in asset["diseaseMechanisms"]}
    for block in blocks:
        if (block.get("kind") != "table"
                or block.get("columns") != ["疾病", "风险指数", "风险等级", "机制关联"]):
            continue
        for row in block.get("rows", []):
            cells = row.get("cells") if isinstance(row, dict) else row
            if (not isinstance(cells, list) or len(cells) != 4
                    or not isinstance(cells[0], str) or cells[3] is not None):
                continue
            mechanism = mechanisms.get((block.get("title"), cells[0]))
            if mechanism is not None:
                cells[3] = mechanism


def _fill_attention_scale(section):
    blocks = []
    scale_seen = False
    for block in section.get("blocks", []):
        if isinstance(block, dict) and block.get("kind") == "paragraph" and block.get("title") == "5.3 风险指数标尺说明":
            if scale_seen:
                continue
            block["text"] = _ATTENTION_SCALE_TEXT
            scale_seen = True
        blocks.append(block)
    section["blocks"] = blocks


def expand_dynamic_report(report: dict, *, template_version: str | None = None,
                          template_archive_dir: str | Path | None = None) -> dict:
    """Return display-only sections without mutating dynamic data or scores.

    v1 and old internal documents retain their existing full presentation.
    Unknown public versions fail explicitly instead of receiving today's copy.
    """
    if not isinstance(report, dict):
        raise TypeError("report must be a dictionary")
    selected_template = resolve_display_template(report, template_version)
    loader_options = {"archive_directory": template_archive_dir} if template_archive_dir is not None else {}
    capabilities = display_capabilities(selected_template)
    refined = capabilities.refined_fixed_layout
    version = report.get("schemaVersion")
    if capabilities.compact_layout and version != DYNAMIC_SCHEMA_VERSION:
        # Explicit opt-in only. New internal reports carry the binding in audit;
        # strip their old full fixed chapters before filling the selected asset.
        if isinstance(version, str) and version.startswith("gut-barrier-public.") and version != "gut-barrier-public.v1":
            raise ValueError(f"unsupported gut barrier public report version: {version}")
        from modules.gut_barrier.publication import project_callback_report
        report = project_callback_report(report, archive_directory=template_archive_dir)
        version = DYNAMIC_SCHEMA_VERSION
    if version != DYNAMIC_SCHEMA_VERSION:
        if isinstance(version, str) and version.startswith("gut-barrier-public.") and version != "gut-barrier-public.v1":
            raise ValueError(f"unsupported gut barrier public report version: {version}")
        output = deepcopy(report)
        # Newly generated full previews also carry the marker. Old v1/internal
        # reports remain byte-for-byte data copies without loading new copy.
        if uses_attention_index(output.get("sections")):
            asset = load_static_template(selected_template, **loader_options)
            for section in output["sections"]:
                if not isinstance(section, dict):
                    continue
                if section.get("id") == "disease_risks":
                    _fill_diseases(section, asset)
                elif section.get("id") == "appendix":
                    _fill_attention_scale(section)
        return output
    if capabilities.canonical_dynamic_layout and not capabilities.product_feedback_layout:
        # Explicit current-preview binding shares the publication projection;
        # clinical titles, cells and ordering are never reinvented by the view.
        from modules.gut_barrier.publication import normalize_current_dynamic_report
        output = normalize_current_dynamic_report(report)
    else:
        output = deepcopy(report)
    asset = load_static_template(selected_template, **loader_options)
    if refined:
        # Private view state is computed here, never accepted from a callback.
        output.pop("_referenceAliases", None)
    sections = output.get("sections")
    if not isinstance(sections, list):
        raise ValueError("dynamic gut barrier report sections must be a list")
    if capabilities.ordered_reference_layout:
        from modules.gut_barrier.reference_order import bind_ordered_static_copy
        asset = bind_ordered_static_copy(
            asset, output, allow_omitted_fixed=capabilities.pinned_reference_layout)
    elif capabilities.product_feedback_layout:
        asset = _patient_first_static_copy(asset, sections)
    by_id = {section.get("id"): section for section in sections if isinstance(section, dict)}
    source_overview = deepcopy(by_id.get("overview") or {}) if capabilities.word_review_layout else {}
    if "overview" in by_id:
        _fill_overview(by_id["overview"], asset,
                       canonical_dynamic_layout=capabilities.canonical_dynamic_layout)
    if "disease_risks" in by_id and not capabilities.canonical_dynamic_layout:
        _fill_diseases(by_id["disease_risks"], asset,
                       clean_review_layout=capabilities.clean_review_layout)
    if "appendix" in by_id:
        aliases = {} if refined else None
        fixed_blocks = asset["appendixBlocks"]
        wire_quality_control = None
        if capabilities.canonical_dynamic_layout:
            for block in by_id["appendix"].get("blocks", []):
                if (not isinstance(block, dict) or block.get("kind") != "table"
                        or block.get("title") != "4.1 质控校验"
                        or block.get("columns") != ["校验项目", "本次记录", "校验项目", "本次记录"]):
                    continue
                rows = block.get("rows")
                if not isinstance(rows, list) or len(rows) != 5:
                    continue
                cells = [row.get("cells") if isinstance(row, dict) else row for row in rows]
                if all(isinstance(row, list) and len(row) == 4
                       and all(value is None or isinstance(value, (str, int, float, bool)) for value in row)
                       for row in cells):
                    wire_quality_control = block
                    break
            # Bind a private fixed copy before merging. The same expanded QC
            # table is then recognized on a second composition, without losing
            # unknown dynamic blocks that happen to use a similar heading.
            fixed_appendix = {"appendix": {"blocks": deepcopy(fixed_blocks)}}
            if wire_quality_control is not None:
                fixed_appendix["appendix"]["blocks"] = [
                    deepcopy(wire_quality_control) if block.get("kind") == "table"
                    and block.get("title") == "4.1 质控校验" else block
                    for block in fixed_appendix["appendix"]["blocks"]]
            else:
                _fill_quality_control(fixed_appendix, source_overview, canonical_dynamic_layout=True)
            fixed_blocks = fixed_appendix["appendix"]["blocks"]
        by_id["appendix"]["blocks"] = (
            _merge_patient_first_references(fixed_blocks, by_id["appendix"].get("blocks", []))
            if capabilities.product_feedback_layout else
            _merge_references(fixed_blocks, by_id["appendix"].get("blocks", []), reference_aliases=aliases))
        if aliases:
            output["_referenceAliases"] = aliases
        if uses_attention_index(sections):
            _fill_attention_scale(by_id["appendix"])
        if capabilities.word_review_layout and not capabilities.canonical_dynamic_layout:
            _fill_quality_control(by_id, source_overview,
                                  canonical_dynamic_layout=capabilities.canonical_dynamic_layout)
    for section in asset["fixedSections"]:
        # A present section is never replaced, even on a malformed v2 document.
        by_id.setdefault(section["id"], deepcopy(section))
    ordered = [by_id[identifier] for identifier in asset["sectionOrder"] if identifier in by_id]
    ordered.extend(section for section in sections if not isinstance(section, dict) or section.get("id") not in asset["sectionOrder"])
    if capabilities.closed_boundary_layout:
        # Presentation only: hide the retired explanation and its appendix
        # duplicate without rewriting frozen callback data or static assets.
        hidden_titles = {"风险指数说明", "状态下限说明", "风险评估结果说明", "风险评估资料提示"}
        for section in ordered:
            if isinstance(section, dict):
                section["blocks"] = [block for block in section.get("blocks", [])
                                     if not isinstance(block, dict) or block.get("title") not in hidden_titles]
    output["sections"] = ordered
    return output


def project_display_report(report: dict, *, template_version: str,
                           template_archive_dir: str | Path | None = None) -> dict:
    """Compose a frontend display bundle, not a callback payload.

    Callers obtain the version from authenticated callback headers or the saved
    render context. This function does not guess it from patient data or dates.
    sourceReport retains the unmodified data consumed by covers and diagrams;
    referenceAliases maps citations after duplicate publications are merged.
    """
    from .graphics import supports_report_graphics
    from .latest_view import arrange_sections, display_blocks
    capabilities = display_capabilities(template_version)
    view = expand_dynamic_report(report, template_version=template_version,
                                 template_archive_dir=template_archive_dir)
    if capabilities.compact_layout:
        sections = arrange_sections(view["sections"], clean_review_layout=capabilities.clean_review_layout,
                                    canonical_dynamic_layout=capabilities.canonical_dynamic_layout)
        if supports_report_graphics(sections, template_version=template_version):
            blocks = display_blocks(sections, xu=capabilities.xu_review,
                                    source_stage_layout=capabilities.source_stage_layout,
                                    word_review_layout=capabilities.word_review_layout,
                                    canonical_dynamic_layout=capabilities.canonical_dynamic_layout,
                                    template_risk_layout=capabilities.template_risk_layout)
            for section in sections:
                if section.get("visible") is False:
                    continue
                section["blocks"] = [blocks[id(block)] for block in section.get("blocks", [])
                                     if blocks.get(id(block)) is not None]
        view["sections"] = sections
    view["referenceAliases"] = view.pop("_referenceAliases", {})
    view["sourceReport"] = deepcopy(report)
    return view
