"""Normalize Markdown prose wrapping and fenced JSON examples.

Ordinary Markdown paragraphs are stored on one physical line. Structural blocks
(headings, lists, tables, quotes, code, HTML, and explicit hard breaks) retain
their Markdown semantics. Valid fenced JSON is rendered with four-space
indentation.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_DIRS = {".git", ".pytest_cache", ".venv", "node_modules"}
FENCE_RE = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})\s*(?P<lang>[\w.+-]*)\s*$")
LIST_RE = re.compile(r"^(?P<indent>\s*)(?:[-+*]|\d+[.)])\s+")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}(?:\s+|$)")
LINK_DEF_RE = re.compile(r"^\s{0,3}\[[^]]+\]:\s*")
TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)
THEMATIC_BREAK_RE = re.compile(
    r"^\s{0,3}(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})$"
)
HTML_START_RE = re.compile(
    r"^\s{0,3}(?:<!--|</?(?:address|article|aside|base|basefont|blockquote|body|"
    r"caption|center|col|colgroup|dd|details|dialog|dir|div|dl|dt|fieldset|"
    r"figcaption|figure|footer|form|frame|frameset|h[1-6]|head|header|hr|html|"
    r"iframe|legend|li|link|main|menu|menuitem|nav|noframes|ol|optgroup|option|"
    r"p|param|search|section|summary|table|tbody|td|tfoot|th|thead|title|tr|"
    r"track|ul)(?:\s|>|/))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class JsonNotice:
    path: Path
    line: int
    message: str


def markdown_files() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*.md")
        if not any(part in EXCLUDED_DIRS for part in path.relative_to(ROOT).parts)
    )


def _table_lines(lines: list[str]) -> set[int]:
    protected: set[int] = set()
    for index, line in enumerate(lines):
        if not TABLE_SEPARATOR_RE.match(line):
            continue
        protected.add(index)
        if index:
            protected.add(index - 1)
        cursor = index + 1
        while cursor < len(lines) and lines[cursor].strip() and "|" in lines[cursor]:
            protected.add(cursor)
            cursor += 1
    return protected


def _join_parts(parts: list[str]) -> str:
    result = parts[0].rstrip()
    for part in parts[1:]:
        value = part.strip()
        if not value:
            continue
        separator = ""
        left = result[-1] if result else ""
        right = value[0]
        joins_chinese_text = (
            "\u3400" <= left <= "\u9fff" and "\u3400" <= right <= "\u9fff"
        )
        closes_chinese_clause = left in "，。；：！？、）】》」”)]}"
        if (
            result
            and not result.endswith((" ", "　"))
            and not joins_chinese_text
            and not closes_chinese_clause
        ):
            separator = " "
        result += separator + value
    return result


def _is_structural(line: str, index: int, lines: list[str], table_lines: set[int]) -> bool:
    stripped = line.strip()
    if index in table_lines:
        return True
    if HEADING_RE.match(line) or LINK_DEF_RE.match(line):
        return True
    if THEMATIC_BREAK_RE.match(line) or stripped in {"$$", ":::", "<!--", "-->"}:
        return True
    if stripped.startswith(("<!--", "-->", "<!DOCTYPE")) or HTML_START_RE.match(line):
        return True
    if stripped.startswith(("::", "!!! ")):
        return True
    if re.match(r"^\s{0,3}\[[ xX]\]\s+", line):
        return True
    if index + 1 < len(lines) and re.match(r"^\s{0,3}(?:=+|-+)\s*$", lines[index + 1]):
        return True
    if re.match(r"^\s{0,3}(?:=+|-+)\s*$", line) and index:
        return True
    return False


def normalize_prose(lines: list[str]) -> list[str]:
    table_lines = _table_lines(lines)
    output: list[str] = []
    paragraph: list[str] = []
    paragraph_kind: str | None = None
    in_fence = False
    fence_char = ""
    fence_length = 0
    in_frontmatter = bool(lines and lines[0].strip() == "---")
    in_html_comment = False
    in_math = False

    def flush() -> None:
        nonlocal paragraph, paragraph_kind
        if paragraph:
            output.append(_join_parts(paragraph))
            paragraph = []
            paragraph_kind = None

    for index, line in enumerate(lines):
        stripped = line.strip()

        if in_frontmatter:
            output.append(line)
            if index and stripped == "---":
                in_frontmatter = False
            continue

        fence_match = FENCE_RE.match(line)
        if in_fence:
            output.append(line)
            if re.match(rf"^\s{{0,3}}{re.escape(fence_char)}{{{fence_length},}}\s*$", line):
                in_fence = False
            continue
        if fence_match:
            flush()
            output.append(line)
            in_fence = True
            fence_char = fence_match.group("fence")[0]
            fence_length = len(fence_match.group("fence"))
            continue

        if in_html_comment:
            output.append(line)
            if "-->" in line:
                in_html_comment = False
            continue
        if "<!--" in line and "-->" not in line:
            flush()
            output.append(line)
            in_html_comment = True
            continue

        if stripped == "$$":
            flush()
            output.append(line)
            in_math = not in_math
            continue
        if in_math:
            output.append(line)
            continue

        if not stripped:
            flush()
            output.append(line)
            continue

        if line.endswith(("  ", "\\")):
            flush()
            output.append(line)
            continue

        quote_match = re.match(r"^(\s{0,3}(?:>\s*)+)(.*)$", line)
        if quote_match:
            prefix, content = quote_match.groups()
            if not content.strip() or _is_structural(content, index, lines, table_lines):
                flush()
                output.append(line)
                continue
            kind = f"quote:{prefix}"
            if paragraph and paragraph_kind != kind:
                flush()
            paragraph_kind = kind
            paragraph.append(prefix + content if not paragraph else content)
            continue

        list_match = LIST_RE.match(line)
        if list_match:
            flush()
            paragraph_kind = f"list:{len(list_match.group('indent'))}"
            paragraph.append(line)
            continue

        if _is_structural(line, index, lines, table_lines):
            flush()
            output.append(line)
            continue

        if line.startswith(("    ", "\t")) and paragraph_kind is None:
            flush()
            output.append(line)
            continue

        if paragraph_kind and paragraph_kind.startswith("quote:"):
            flush()
        paragraph_kind = paragraph_kind or "prose"
        paragraph.append(line)

    flush()
    return output


def _without_trailing_commas(value: str) -> str:
    output: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(value):
        char = value[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == ",":
            cursor = index + 1
            while cursor < len(value) and value[cursor].isspace():
                cursor += 1
            if cursor < len(value) and value[cursor] in "}]":
                index += 1
                continue
        output.append(char)
        index += 1
    return "".join(output)


def _decode_multiple(value: str) -> list[object] | None:
    decoder = json.JSONDecoder()
    decoded: list[object] = []
    cursor = 0
    while cursor < len(value):
        while cursor < len(value) and value[cursor].isspace():
            cursor += 1
        if cursor == len(value):
            break
        try:
            item, cursor = decoder.raw_decode(value, cursor)
        except json.JSONDecodeError:
            return None
        decoded.append(item)
    return decoded if len(decoded) > 1 else None


def _lexical_json_indent(value: str) -> list[str]:
    lines: list[str] = []
    current = ""
    depth = 0
    in_string = False
    escaped = False

    def emit() -> None:
        nonlocal current
        if current.strip():
            lines.append(" " * (depth * 4) + current.strip())
        current = ""

    for char in value.strip():
        if in_string:
            current += char
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            current += char
        elif char in "[{":
            if current.strip():
                current = current.rstrip() + char
            else:
                current = char
            emit()
            depth += 1
        elif char in "]}":
            emit()
            depth = max(0, depth - 1)
            current = char
        elif char == ",":
            current = current.rstrip() + char
            emit()
        elif char == ":":
            current = current.rstrip() + ": "
        elif char.isspace():
            if current and not current.endswith(" "):
                current += " "
        else:
            if depth == 0 and current.strip() in {"}", "]"}:
                emit()
            current += char
    emit()
    return lines


def _format_json_chunk(value: str) -> tuple[list[str], bool]:
    cleaned = _without_trailing_commas(value.strip())
    candidates = [(cleaned, False)]
    if re.match(r'^"(?:[^"\\]|\\.)+"\s*:', cleaned):
        candidates.insert(0, ("{" + cleaned + "}", True))

    for candidate, _wrapped in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        return json.dumps(parsed, ensure_ascii=False, indent=4).splitlines(), candidate != value.strip()

    multiple = _decode_multiple(cleaned)
    if multiple is not None:
        rendered: list[str] = []
        for item in multiple:
            if rendered:
                rendered.append("")
            rendered.extend(json.dumps(item, ensure_ascii=False, indent=4).splitlines())
        return rendered, True

    return _lexical_json_indent(value), True


def _format_json_like(value: str) -> tuple[list[str], bool]:
    comment_re = re.compile(r"^\s*//")
    if not any(comment_re.match(line) for line in value.splitlines()):
        return _format_json_chunk(value)

    rendered: list[str] = []
    chunk: list[str] = []
    nonstandard = True

    def flush_chunk() -> None:
        nonlocal chunk
        raw = "\n".join(chunk).strip()
        if raw:
            if rendered and rendered[-1] != "":
                rendered.append("")
            formatted, _ = _format_json_chunk(raw)
            rendered.extend(formatted)
        chunk = []

    for line in value.splitlines():
        if comment_re.match(line):
            flush_chunk()
            if rendered and rendered[-1] != "":
                rendered.append("")
            rendered.append(line.strip())
        else:
            chunk.append(line)
    flush_chunk()
    return rendered, nonstandard


def normalize_json_fences(lines: list[str], path: Path) -> tuple[list[str], list[JsonNotice], int]:
    output: list[str] = []
    notices: list[JsonNotice] = []
    formatted = 0
    index = 0

    while index < len(lines):
        opening = FENCE_RE.match(lines[index])
        if not opening or opening.group("lang").lower() != "json":
            output.append(lines[index])
            index += 1
            continue

        output.append(lines[index])
        fence = opening.group("fence")
        fence_char = fence[0]
        fence_length = len(fence)
        closing_re = re.compile(rf"^\s{{0,3}}{re.escape(fence_char)}{{{fence_length},}}\s*$")
        closing = index + 1
        while closing < len(lines) and not closing_re.match(lines[closing]):
            closing += 1
        if closing == len(lines):
            notices.append(JsonNotice(path, index + 1, "JSON code fence is not closed"))
            output.extend(lines[index + 1 :])
            break

        raw_lines = lines[index + 1 : closing]
        raw = "\n".join(raw_lines).strip()
        if not raw:
            notices.append(JsonNotice(path, index + 1, "JSON code fence is empty"))
            output.extend(raw_lines)
        else:
            rendered, nonstandard = _format_json_like(raw)
            indent = opening.group("indent")
            output.extend(indent + line if line else line for line in rendered)
            formatted += 1
            if nonstandard:
                notices.append(
                    JsonNotice(path, index + 1, "formatted non-standard JSON example")
                )

        output.append(lines[closing])
        index = closing + 1

    return output, notices, formatted


def normalize_document(text: str, path: Path) -> tuple[str, list[JsonNotice], int]:
    had_final_newline = text.endswith(("\n", "\r"))
    lines = text.splitlines()
    lines, problems, formatted = normalize_json_fences(lines, path)
    lines = normalize_prose(lines)
    result = "\n".join(lines)
    if had_final_newline:
        result += "\n"
    return result, problems, formatted


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="report files that need formatting")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="list legacy JSON-like examples that require tolerant formatting",
    )
    args = parser.parse_args()

    changed: list[Path] = []
    notices: list[JsonNotice] = []
    json_blocks = 0
    for path in markdown_files():
        original = path.read_text(encoding="utf-8")
        normalized, current_problems, current_blocks = normalize_document(original, path)
        notices.extend(current_problems)
        json_blocks += current_blocks
        if normalized == original.replace("\r\n", "\n"):
            continue
        changed.append(path)
        if not args.check:
            path.write_text(normalized, encoding="utf-8", newline="")

    for notice in notices:
        if not args.verbose and notice.message == "formatted non-standard JSON example":
            continue
        relative = notice.path.relative_to(ROOT)
        print(f"{relative}:{notice.line}: {notice.message}", file=sys.stderr)
    mode = "need formatting" if args.check else "formatted"
    print(
        f"{len(changed)} Markdown files {mode}; "
        f"{json_blocks} JSON blocks; {len(notices)} legacy JSON-like blocks"
    )
    if any(notice.message != "formatted non-standard JSON example" for notice in notices):
        return 2
    if args.check and changed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
