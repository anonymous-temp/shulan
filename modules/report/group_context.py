"""Source-bound OpenIM records for evaluate, independent of HTTP transport."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlsplit


@dataclass(frozen=True)
class GroupAttachment:
    url: str
    file_type: str | None
    message_id: str
    sender_id: str
    send_time: int | None
    content_type: int


@dataclass
class GroupContextResult:
    patient_messages: list[dict[str, Any]] = field(default_factory=list)
    other_messages: list[dict[str, Any]] = field(default_factory=list)
    attachments: list[GroupAttachment] = field(default_factory=list)
    source_errors: list[str] = field(default_factory=list)


class GroupContextReader(Protocol):
    async def read(self, *, user_id: str, group_id: str) -> GroupContextResult: ...


def _time(value):
    # Booleans and seconds/floats are not the declared Unix millisecond type.
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _identifier(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def attachment_file_type(row, content_type):
    """Message type selects the branch; metadata only refines a file's format."""
    if content_type == 102:
        return 'image'
    for name in ('mimeType', 'contentMimeType', 'fileMimeType', 'fileType'):
        hint = str(row.get(name) or '').strip().lower()
        if hint in {'pdf', 'application/pdf'}:
            return 'pdf'
        if hint == 'image' or hint.startswith('image/'):
            return 'image'
    # /download itself is not a filename. parse_qs performs the one permitted
    # parameter decoding; do not unquote that result a second time.
    try:
        parsed = urlsplit(row['content'])
        query = parse_qs(parsed.query)
        name = (query.get('objectName') or [None])[0]
        name = name if name is not None else unquote(parsed.path.rsplit('/', 1)[-1])
    except (TypeError, ValueError):
        return None
    suffix = str(name).lower().rsplit('.', 1)[-1]
    if suffix == 'pdf':
        return 'pdf'
    if suffix in {'jpg', 'jpeg', 'png', 'webp', 'gif', 'bmp', 'tif', 'tiff'}:
        return 'image'
    return None


def parse_group_context(body, *, user_id, group_id, max_messages=500, max_text_chars=200_000):
    """Decode exactly the documented response, isolating malformed messages.

    No IM member is inferred to be a clinician or patient from their nickname.
    Only an exact business ID, or its exact OpenIM base64 representation,
    establishes a patient message. A repeated/revoked message is not a second
    or continuing health statement.
    """
    result = GroupContextResult()
    if not isinstance(body, dict):
        result.source_errors.append('GROUP_RESPONSE_INVALID')
        return result
    response = body.get('results')
    if (body.get('state') not in (None, 200, '200') or body.get('code') not in (None, 0, 200, '0', '200')
            or not isinstance(response, dict) or response.get('errCode') not in (None, 0, '0')
            or not isinstance(response.get('data'), dict)):
        result.source_errors.append('GROUP_RESPONSE_UNAVAILABLE')
        return result
    data = response['data']
    rows = data.get('chatLogs')
    if not isinstance(rows, list):
        result.source_errors.append('GROUP_RESPONSE_INVALID')
        return result
    if any(owner.get('hasMore') is True or owner.get('isLastPage') is False
            or owner.get('nextCursor') not in (None, '') for owner in (body, response, data)):
        result.source_errors.append('GROUP_HISTORY_INCOMPLETE')
    # chatLogsNum is the documented returned count, never a total-page count.
    if data.get('chatLogsNum') not in (None, len(rows)):
        result.source_errors.append('GROUP_HISTORY_COUNT_MISMATCH')
    total = body.get('total')
    if isinstance(total, int) and not isinstance(total, bool) and total > len(rows):
        result.source_errors.append('GROUP_HISTORY_INCOMPLETE')
    patient_ids = {user_id, base64.b64encode(user_id.encode()).decode()}
    normalized, revoked_servers, revoked_clients, revoked_aliases = [], set(), set(), set()
    server_clients = set()
    for index, item in enumerate(rows):
        if not isinstance(item, dict) or not isinstance(item.get('chatLog'), dict):
            result.source_errors.append('GROUP_MESSAGE_INVALID')
            continue
        row = item['chatLog']
        server_id, client_id = _identifier(row.get('serverMsgID')), _identifier(row.get('clientMsgID'))
        supplied_group = row.get('groupID')
        if supplied_group not in (None, '', group_id):
            result.source_errors.append('GROUP_MESSAGE_GROUP_MISMATCH')
            continue
        if server_id and client_id:
            server_clients.add(client_id)
        if item.get('isRevoked') is True:
            if server_id:
                revoked_servers.add(server_id)
            elif client_id:
                revoked_clients.add(client_id)
            if client_id:
                revoked_aliases.add(client_id)
            continue
        if item.get('isRevoked') not in (None, False):
            result.source_errors.append('GROUP_MESSAGE_INVALID')
            continue
        content_type, content = row.get('contentType'), row.get('content')
        if content_type not in (101, 102, 105) or isinstance(content_type, bool):
            continue  # System/event messages have no patient observation role.
        if not isinstance(content, str) or not content.strip() or not (server_id or client_id):
            result.source_errors.append('GROUP_MESSAGE_INVALID')
            continue
        sender = _identifier(row.get('sendID')) or ''
        normalized.append((index, row, server_id, client_id, sender, _time(row.get('sendTime'))))
    normalized.sort(key=lambda entry: (entry[5] is None, entry[5] or 0, entry[0]))
    retained, seen = [], set()
    for entry in normalized:
        server_id, client_id = entry[2:4]
        if (server_id in revoked_servers or client_id in revoked_clients
                or not server_id and (client_id in revoked_aliases or client_id in server_clients)):
            continue
        key = ('server', server_id) if server_id else ('client', client_id)
        if key not in seen:
            retained.append(entry)
            seen.add(key)
    if len(retained) > max_messages:
        # Retain the latest bounded source window; absent timestamps do not
        # establish recency and therefore keep their original final order.
        retained = retained[-max_messages:]
        result.source_errors.append('GROUP_HISTORY_TRUNCATED')
    attachment_urls, text_size = set(), 0
    for _, row, server_id, client_id, sender, sent in retained:
        message_id = server_id or client_id
        content_type, content = row['contentType'], row['content']
        if content_type == 101:
            if text_size + len(content) > max_text_chars:
                result.source_errors.append('GROUP_TEXT_TRUNCATED')
                continue
            text_size += len(content)
            record = dict(role='user' if sender in patient_ids else 'other', content=content,
                recordId='group:'+group_id+':'+message_id, sendID=sender,
                sendTime=sent, source='group:'+group_id)
            (result.patient_messages if sender in patient_ids else result.other_messages).append(record)
        else:
            # Distinct messages remain in the source record lane; the same
            # exact resource need only be parsed once within this group read.
            if content in attachment_urls:
                continue
            attachment_urls.add(content)
            result.attachments.append(GroupAttachment(content, attachment_file_type(row, content_type),
                message_id, sender, sent, content_type))
    result.source_errors = list(dict.fromkeys(result.source_errors))
    return result
