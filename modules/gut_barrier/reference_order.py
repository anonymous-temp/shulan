"""One frozen citation namespace for a dynamic report and its fixed release.

This is reference metadata composition, not HTML rendering. The callback keeps
only patient-dependent body blocks; its bibliography also binds the sources
cited by the immutable client-owned copy. An explicit release can also publish
a reviewed, user-requested bibliography prefix without inventing body citations.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
import math
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit, urlunsplit

from .display_versions import (
    ORDERED_REFERENCE_DISPLAY_TEMPLATE, TEMPLATE_RISK_DISPLAY_TEMPLATE,
    PINNED_REFERENCE_DISPLAY_TEMPLATE, CLOSED_BOUNDARY_DISPLAY_TEMPLATE, BARRIER_ONLY_DISPLAY_TEMPLATE,
)


PRIMARY_TITLE = "4.4 参考文献"
SUPPLEMENT_TITLE = "4.5 补充参考资料"
REFERENCE_FIELDS = ("citation", "url", "year", "publisher", "doi", "location")
_CITATION = re.compile(r"\[(?P<group>补充|补)?(?P<numbers>\d+(?:\s*[-–—,，、]\s*\d+)*)\]")
_NAMED = re.compile(r"^\[评估\d+\]\s*")
REFERENCE_BUDGET_VERSION = "gut-barrier-selected-references-v1.20261007"
PINNED_BUDGET_VERSION = "gut-barrier-selected-references-v2.20261008"
PUBLIC_REFERENCE_LIMIT = 15
PUBLIC_REFERENCE_TARGET = (12, 15)


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_asset(path, version):
    asset = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(asset, dict) or asset.get("templateVersion") != version
            or asset.get("reportSchemaVersion") != "gut-barrier-public.v2"):
        raise ValueError("ordered reference asset version mismatch")
    return asset


@lru_cache(maxsize=2)
def _read_asset(version):
    if version not in {CLOSED_BOUNDARY_DISPLAY_TEMPLATE, BARRIER_ONLY_DISPLAY_TEMPLATE}:
        raise ValueError("ordered references require their exact release asset")
    path = _PROJECT_ROOT / "adapters" / "gut_barrier" / "assets" / f"{version}.json"
    return _load_asset(path, version)


def _archived_reference_path(version, archive_directory):
    if not archive_directory:
        raise FileNotFoundError(
            f"historical gut barrier reference template {version} is archived; supply "
            "archive_directory explicitly with its directory outside the project")
    directory = Path(archive_directory).expanduser()
    if not directory.is_absolute():
        raise ValueError("gut barrier template archive directory must be an absolute path outside the project")
    resolved = directory.resolve()
    if any(path == _PROJECT_ROOT or _PROJECT_ROOT in path.parents for path in (directory, resolved)):
        raise ValueError("gut barrier template archive must be outside the project directory")
    path = (resolved / f"{version}.json").resolve()
    if resolved not in path.parents or path == _PROJECT_ROOT or _PROJECT_ROOT in path.parents:
        raise ValueError("archived gut barrier template must stay inside its external archive directory")
    if not path.is_file():
        raise FileNotFoundError(f"archived gut barrier reference template {version} not found: {path}")
    return path


def load_reference_asset(version, *, archive_directory: str | Path | None = None):
    """Read the exact release; only explicit offline callers may load archives.

    Current generation always uses its repository asset. Historical projection
    has no automatic archive fallback, and archives are not cached across calls.
    Already frozen callbacks do not call this loader.
    """
    if version in {CLOSED_BOUNDARY_DISPLAY_TEMPLATE, BARRIER_ONLY_DISPLAY_TEMPLATE}:
        return deepcopy(_read_asset(version))
    if version not in {ORDERED_REFERENCE_DISPLAY_TEMPLATE, TEMPLATE_RISK_DISPLAY_TEMPLATE,
                       PINNED_REFERENCE_DISPLAY_TEMPLATE}:
        raise ValueError("ordered references require their exact release asset")
    return _load_asset(_archived_reference_path(version, archive_directory), version)


def _doi(value):
    value = unquote(str(value or "")).strip().casefold()
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value)
    return value.rstrip(" .") if value.startswith("10.") and "/" in value else None


def reference_keys(item):
    """Exact normalized publication identities, never fuzzy title similarity."""
    keys = set()
    if doi := _doi(item.get("doi")):
        keys.add(("doi", doi))
    url = item.get("url")
    if isinstance(url, str):
        try:
            parsed = urlsplit(url.strip())
            if parsed.scheme in {"https", "http"} and parsed.hostname:
                host, path = parsed.hostname.casefold(), parsed.path.rstrip("/")
                keys.add(("url", urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), path, parsed.query, ""))))
                if host in {"doi.org", "dx.doi.org"} and (doi := _doi(path.lstrip("/"))):
                    keys.add(("doi", doi))
                if (host, path) in {
                    ("www.who.int", "/publications/i/item/9789240015128"),
                    ("iris.who.int", "/bitstream/handle/10665/336656/9789240015128-eng.pdf"),
                }:
                    keys.add(("edition", "who-physical-activity-2020-9789240015128"))
        except ValueError:
            pass
    if not keys and item.get("citation"):
        keys.add(("citation", str(item["citation"]).strip()))
    return keys


def _clean(item):
    result = {key: deepcopy(item.get(key)) for key in REFERENCE_FIELDS}
    if isinstance(result["citation"], str):
        result["citation"] = _NAMED.sub("", result["citation"])
    return result


class _Pool:
    def __init__(self):
        self.items, self.parents, self.identities = [], [], {}

    def root(self, index):
        while self.parents[index] != index:
            self.parents[index] = self.parents[self.parents[index]]
            index = self.parents[index]
        return index

    def add(self, item):
        item = _clean(item)
        index = len(self.items)
        self.items.append(item)
        self.parents.append(index)
        keys = reference_keys(item)
        matches = {self.root(self.identities[key]) for key in keys if key in self.identities}
        if matches:
            root = min(matches)
            self.parents[index] = root
            for other in matches:
                self.parents[other] = root
        for key in keys:
            self.identities[key] = index
        return index

    def item(self, root):
        # Every original source remains untouched in its internal/static
        # catalog. Equivalent copies enrich missing bibliographic fields only.
        merged = dict.fromkeys(REFERENCE_FIELDS)
        for index, item in enumerate(self.items):
            if self.root(index) != root:
                continue
            for key, value in item.items():
                if merged[key] is None or merged[key] == "":
                    merged[key] = deepcopy(value)
        return merged


def _reference_blocks(report):
    return [block for section in report.get("sections", [])
            if isinstance(section, dict) and section.get("visible") is not False
            for block in section.get("blocks", [])
            if isinstance(block, dict) and block.get("kind") == "references"]


def _fixed_groups(asset):
    groups = [block for block in asset["appendixBlocks"] if block.get("kind") == "references"]
    primary = next((block for block in groups if "参考文献" in str(block.get("title"))), None)
    return {"primary": primary.get("items", []) if primary else [],
            "supplement": [item for block in groups if block is not primary for item in block.get("items", [])]}


def _numbers(match, groups):
    items = groups["supplement" if match.group("group") else "primary"]
    resolved = []
    for part in re.split(r"[,，、]", match.group("numbers")):
        bounds = re.split(r"[-–—]", part)
        if len(bounds) > 2 or any(len(value.strip()) > 5 for value in bounds):
            return None
        start, end = int(bounds[0]), int(bounds[-1])
        if start < 1 or end < start or end > len(items) or end - start > 100:
            return None
        resolved.extend(items[index - 1] for index in range(start, end + 1))
    return list(dict.fromkeys(resolved))


def _map_block(block, rewrite):
    """Only fields actually rendered as citations, never metadata or titles."""
    kind = block.get("kind")
    if kind == "table":
        for row in block.get("rows", []):
            cells = row.get("cells", []) if isinstance(row, dict) else row
            for index, value in enumerate(cells):
                if isinstance(value, str):
                    cells[index] = rewrite(value)
    elif kind == "list":
        block["items"] = [rewrite(value) if isinstance(value, str) else value for value in block.get("items", [])]
    elif kind not in {"references", "facts", "cards", "bands"} and isinstance(block.get("text"), str):
        block["text"] = rewrite(block["text"])


def _composed_blocks(report, asset):
    """The canonical release order, without importing any preview package."""
    sections = {section.get("id"): section for section in report.get("sections", []) if isinstance(section, dict)}
    fixed = {section["id"]: section for section in asset["fixedSections"]}
    order = list(dict.fromkeys([*asset["sectionOrder"], *sections]))
    for identifier in order:
        section = sections.get(identifier, fixed.get(identifier, {}))
        if section.get("visible") is False:
            continue
        origin = "dynamic" if identifier in sections else "fixed"
        blocks = [(block, origin) for block in section.get("blocks", [])]
        if identifier == "overview":
            for insertion in asset["overviewInsertions"]:
                block = insertion["block"]
                if any(block == current for current, _ in blocks):
                    continue
                anchor = next((i for i, (current, _) in enumerate(blocks)
                               if all(current.get(key) == value for key, value in insertion["anchor"].items())), None)
                if anchor is not None:
                    blocks.insert(anchor + (insertion["position"] == "after"), (block, "fixed"))
        elif identifier == "appendix":
            dynamic = [(block, origin) for block, origin in blocks if block.get("kind") != "references"]
            quality = next((block for block, _ in dynamic if block.get("kind") == "table"
                            and block.get("title") == "4.1 质控校验"), None)
            blocks = [(quality, "dynamic") if quality is not None and block.get("title") == "4.1 质控校验"
                      else (block, "fixed") for block in asset["appendixBlocks"] if block.get("kind") != "references"]
            blocks.extend((block, origin) for block, origin in dynamic if all(block != current for current, _ in blocks))
        yield from blocks


def _reference_priority(item):
    """Prefer exact reviewed supplied identities, not a guessed journal rank."""
    from .reference_priority import reference_selection_audit
    reviewed = reference_selection_audit()["sources"].values()
    keys = reference_keys(item)
    if any(keys & reference_keys(candidate) for candidate in reviewed):
        return 0
    try:
        host = urlsplit(str(item.get("url") or "")).hostname or ""
    except ValueError:
        host = ""
    # Official clinical/nutrition material has identifiable institutional
    # provenance. Unknown studies are not labelled high-quality by journal name.
    if host in {"www.who.int", "iris.who.int", "www.nice.org.uk", "gastro.org",
                "www.fda.gov", "ods.od.nih.gov", "www.cdc.gov", "www.heart.org"}:
        return 1
    from .risk_model import mechanism_evidence
    human_studies = {"ibs_permeability", "human_adipose", "lipids", "urate",
                     "alzheimers", "mood_biomarkers", "rheumatoid_arthritis", "lung_cancer"}
    if any(study["id"] in human_studies and keys & reference_keys(study)
           for entry in mechanism_evidence() for study in entry["studies"]):
        return 2
    return 3


def _publication_claims(report, asset, groups, pool):
    """Inventory the original citations and text before display selection."""
    claims = []
    for ordinal, (block, origin) in enumerate(_composed_blocks(report, asset)):
        def collect(text, **context):
            roots = []
            for match in _CITATION.finditer(text):
                values = _numbers(match, groups[origin])
                if values is not None:
                    roots.extend(pool.root(value) for value in values)
            roots = list(dict.fromkeys(roots))
            if roots:
                claims.append({"origin": origin, "blockOrdinal": ordinal,
                               "blockTitle": block.get("title"), "text": text,
                               "roots": roots, **context})
            return text
        if block.get("kind") == "table":
            disease_table = block.get("columns") == ["疾病", "风险指数", "风险等级", "机制关联"]
            for row_index, row in enumerate(block.get("rows", [])):
                cells = row.get("cells", []) if isinstance(row, dict) else row
                for cell_index, value in enumerate(cells):
                    if not isinstance(value, str):
                        continue
                    context = {"rowIndex": row_index, "cellIndex": cell_index}
                    if disease_table and len(cells) == 4 and cell_index == 3:
                        score = cells[1]
                        if (type(score) in {int, float} and math.isfinite(score)
                                and 0 <= score <= 10 and cells[2] in {"低", "中", "高"}):
                            context.update(disease=cells[0], riskIndex=score)
                    collect(value, **context)
        else:
            _map_block(deepcopy(block), collect)
    return claims


def _select_reference_roots(pool, ordered, claims, limit, *, pinned=(),
                            allow_fixed_budget_omission=False):
    if limit is None:
        return set(ordered)
    if type(limit) is not int or limit < 1:
        raise ValueError("reference display limit must be a positive integer")
    mandatory = set(pinned)
    if not allow_fixed_budget_omission:
        mandatory.update(root for claim in claims if claim["origin"] == "fixed" for root in claim["roots"])
    if len(mandatory) > limit:
        # A fixed client-owned citation cannot be removed by the API. Fail
        # explicitly instead of publishing a dangling static reference.
        raise ValueError("pinned or fixed body references exceed reference display limit" if pinned else
                         "fixed body references exceed reference display limit")
    if len(ordered) <= limit:
        return set(ordered)
    selected = set(mandatory)
    dynamic = [claim for claim in claims if claim["origin"] == "dynamic"]
    coverage = claims if allow_fixed_budget_omission else dynamic
    ranked = sorted((claim for claim in dynamic if "riskIndex" in claim),
                    key=lambda claim: -claim["riskIndex"])
    focus = {id(claim) for claim in ranked[:3]}
    position = {root: index for index, root in enumerate(ordered)}
    priorities = {root: _reference_priority(pool.item(root)) for root in ordered}
    while len(selected) < limit:
        remaining = set(ordered) - selected
        if not remaining:
            break

        def relevance(root):
            uncovered = [claim for claim in coverage if root in claim["roots"]
                         and not selected.intersection(claim["roots"])]
            focused = sum(id(claim) in focus for claim in uncovered)
            # First preserve a source for the patient's three highest scored
            # mechanisms, then exact supplied verification and broad coverage.
            # Neither selection nor citation count changes the risk score.
            return (focused, priorities[root] == 0, len(uncovered),
                    -priorities[root], -position[root])

        selected.add(max(remaining, key=relevance))
    return selected


def publish_ordered_references(report, source, asset, *, reference_limit=None,
                               reference_audit=None, pinned_references=(),
                               allow_fixed_budget_omission=False):
    """Freeze first-citation numbers in-place after clinical projection.

    Returns the COMPLETE source-use inventory for diagnostics. An explicit
    display budget selects visible citations only; it never removes mechanism
    text or claims that the selected sources cover every original statement.
    The optional audit sink is private and never added to the public schema.
    """
    if reference_audit is not None and not isinstance(reference_audit, dict):
        raise TypeError("reference audit must be a dictionary")
    pool = _Pool()
    pinned_items = list(pinned_references)
    pinned_handles = [pool.add(item) for item in pinned_items]
    dynamic_items = [item for block in _reference_blocks(report) for item in block.get("items", [])]
    groups = {"dynamic": {"primary": [pool.add(item) for item in dynamic_items], "supplement": []}}
    inventory = [("pinned", index, item) for index, item in enumerate(pinned_items, 1)]
    inventory.extend(("dynamic", index, item) for index, item in enumerate(dynamic_items, 1))
    # Keep an internal inventory of sources excluded from the patient body.
    # Do not manufacture body citations merely to publish unused sources.
    for block in _reference_blocks(source):
        for index, item in enumerate(block.get("items", []), 1):
            pool.add(item)
            inventory.append((str(block.get("title") or "source"), index, item))
    groups["fixed"] = {name: [pool.add(item) for item in items]
                       for name, items in _fixed_groups(asset).items()}
    inventory.extend((f"fixed-{name}", index, item) for name, items in _fixed_groups(asset).items()
                     for index, item in enumerate(items, 1))
    claims = _publication_claims(report, asset, groups, pool)
    encountered = list(dict.fromkeys(root for claim in claims for root in claim["roots"]))
    pinned = list(dict.fromkeys(pool.root(handle) for handle in pinned_handles))
    candidates = list(dict.fromkeys(pinned + encountered))
    selected = _select_reference_roots(pool, candidates, claims, reference_limit, pinned=pinned,
                                     allow_fixed_budget_omission=allow_fixed_budget_omission)
    numbered = {root: index for index, root in enumerate(
        (root for root in candidates if root in selected), 1)}

    def resolve(text, origin):
        def replace(match):
            values = _numbers(match, groups[origin])
            if values is None:
                return match.group()
            roots = list(dict.fromkeys(pool.root(value) for value in values))
            retained = [root for root in roots if root in numbered]
            return ("[" + ",".join(str(numbered[root]) for root in retained) + "]"
                    if retained else "")
        return _CITATION.sub(replace, text)

    for section in report["sections"]:
        if section.get("visible") is not False:
            for block in section.get("blocks", []):
                _map_block(block, lambda text: resolve(text, "dynamic"))
    main = [pool.item(root) for root in numbered]
    appendix = next(section for section in report["sections"] if section.get("id") == "appendix")
    appendix["blocks"] = [block for block in appendix["blocks"] if block.get("kind") != "references"] + [
        {"kind": "references", "title": PRIMARY_TITLE, "items": main},
    ]
    complete = []
    for origin, index, item in inventory:
        root = next((pool.root(pool.identities[key]) for key in reference_keys(_clean(item))
                     if key in pool.identities), None)
        number = numbered.get(root)
        complete.append({"origin": origin, "sourceIndex": index, "reference": deepcopy(item),
                         "sourceRoot": f"R{root + 1}" if root is not None else None,
                         "bodyNumber": number,
                         "selection": "pinned" if root in pinned else "selected" if number is not None else
                         "budget_omitted" if root in encountered else "uncited",
                         "omissionReason": "selected_display_budget" if root in encountered and number is None
                         else "not_cited_in_rendered_body" if number is None else None})
    if reference_audit is not None:
        reference_audit["referencePublication"] = {
            "version": PINNED_BUDGET_VERSION if pinned else REFERENCE_BUDGET_VERSION, "limit": reference_limit,
            "targetRange": list(PUBLIC_REFERENCE_TARGET), "displayedCount": len(main),
            "originalCitedCount": len(encountered),
            "fixedRequiredCount": (0 if allow_fixed_budget_omission else
                                   len({root for claim in claims if claim["origin"] == "fixed"
                                        for root in claim["roots"]})),
            "mode": "pinned_prefix_selected_display_full_sources_retained" if pinned else
            "selected_display_full_sources_retained" if reference_limit is not None
            else "all_cited_sources",
            "inventory": deepcopy(complete),
            "claims": [{**{key: deepcopy(value) for key, value in claim.items() if key != "roots"},
                        "sourceRoots": [f"R{root + 1}" for root in claim["roots"]],
                        "selectedBodyNumbers": [numbered[root] for root in claim["roots"] if root in numbered]}
                       for claim in claims],
        }
        if pinned:
            reference_audit["referencePublication"].update(
                pinnedCount=len(pinned), pinnedBodyNumbers=[numbered[root] for root in pinned],
                fixedCitedCount=len({root for claim in claims if claim["origin"] == "fixed"
                                    for root in claim["roots"]}),
                fixedBudgetOmissionAllowed=allow_fixed_budget_omission,
                belowTargetReason="insufficient_distinct_verified_sources" if len(main) < PUBLIC_REFERENCE_TARGET[0]
                else None)
    return complete


def bind_ordered_static_copy(asset, report, *, allow_omitted_fixed=False):
    """Map fixed citations to frozen callback numbers; never invent a number."""
    asset = deepcopy(asset)
    blocks = _reference_blocks(report)
    primary = next((block for block in blocks if block.get("title") == PRIMARY_TITLE), None)
    supplement = next((block for block in blocks if block.get("title") == SUPPLEMENT_TITLE), None)
    if primary is None:
        raise ValueError("v10 display requires a frozen main reference block")
    if allow_omitted_fixed:
        # Only the new release permits omission. A receipt must carry its
        # frozen prefix; never repair it from today's catalog in the renderer.
        prefix = asset.get("referencePolicy", {}).get("pinnedPrefix", [])
        if (asset.get("templateVersion") not in {PINNED_REFERENCE_DISPLAY_TEMPLATE, CLOSED_BOUNDARY_DISPLAY_TEMPLATE, BARRIER_ONLY_DISPLAY_TEMPLATE}
                or not prefix
                or len(primary["items"]) < len(prefix)
                or any(not reference_keys(expected).intersection(reference_keys(actual))
                       for expected, actual in zip(prefix, primary["items"]))):
            raise ValueError("pinned reference prefix does not match frozen release")
    # Resolve the fixed catalog's identity bridges before looking up frozen
    # numbers (for example a DOI entry connecting two exact publication URLs).
    pool = _Pool()
    main_handles = [pool.add(item) for item in primary["items"]]
    fixed_handles = {name: [pool.add(item) for item in items] for name, items in _fixed_groups(asset).items()}
    frozen = {pool.root(handle): index for index, handle in enumerate(main_handles, 1)}
    if len(frozen) != len(main_handles):
        raise ValueError("frozen primary references contain duplicate publications")
    groups = {name: [frozen.get(pool.root(handle)) for handle in handles] for name, handles in fixed_handles.items()}
    active = {id(block) for block, origin in _composed_blocks(report, asset) if origin == "fixed"}

    def rewrite(text, *, required):
        def replace(match):
            values = _numbers(match, groups)
            if values is None:
                return match.group()
            if None in values:
                if required and not allow_omitted_fixed:
                    raise ValueError("fixed body citation missing from frozen primary references")
                if not allow_omitted_fixed:
                    return match.group()
                values = [value for value in values if value is not None]
                if not values:
                    return ""
            return "[" + ",".join(map(str, values)) + "]"
        return _CITATION.sub(replace, text)

    for insertion in asset["overviewInsertions"]:
        block = insertion["block"]
        _map_block(block, lambda text: rewrite(text, required=id(block) in active))
    for section in asset["fixedSections"]:
        for block in section["blocks"]:
            _map_block(block, lambda text: rewrite(text, required=id(block) in active))
    for block in asset["appendixBlocks"]:
        _map_block(block, lambda text: rewrite(text, required=id(block) in active))
    asset["appendixBlocks"] = [block for block in asset["appendixBlocks"] if block.get("kind") != "references"] + [
        deepcopy(primary)]
    # Preserve already frozen receipts; newly published reports omit this block.
    if supplement is not None:
        asset["appendixBlocks"].append(deepcopy(supplement))
    return asset
