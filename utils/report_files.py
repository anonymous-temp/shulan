"""Pure filename and category helpers shared by report ingestion paths."""

from urllib.parse import parse_qs
from urllib.parse import urlparse

IMAGE_EXTENSIONS = frozenset({'jpg', 'jpeg', 'png', 'bmp', 'gif'})
REPORT_EXTENSIONS = IMAGE_EXTENSIONS | {'pdf'}


def resolve_report_filename(url: str) -> tuple[str | None, str | None]:
    """Prefer the file service's query filename, then the URL path."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    name = None
    for key in ('objectName', 'fileName', 'filename', 'name'):
        if query.get(key):
            name = query[key][0]
            break
    if not name:
        name = parsed.path.rsplit('/', 1)[-1] or None
    extension = name.rsplit('.', 1)[-1].lower() if name and '.' in name else None
    return name, extension


def report_file_category(extension: str | None) -> str | None:
    if extension == 'pdf':
        return 'pdf'
    if extension in IMAGE_EXTENSIONS:
        return 'image'
    return None


def normalize_legacy_report_files(value):
    """Adapt stored sessions only; never use this to validate public requests."""
    if not isinstance(value, list):
        return value
    result = []
    for item in value:
        if not isinstance(item, dict):
            result.append(item)
            continue
        copied = dict(item)
        file_type = copied.get('fileType')
        if isinstance(file_type, str):
            file_type = file_type.strip().lower()
        elif file_type is None and isinstance(copied.get('url'), str):
            try:
                _, file_type = resolve_report_filename(copied['url'])
            except ValueError:
                # Let the report schema discard invalid historical data;
                # malformed URLs must not abort all session fallbacks.
                pass
        if isinstance(file_type, str):
            copied['fileType'] = report_file_category(file_type) or file_type
        result.append(copied)
    return result
