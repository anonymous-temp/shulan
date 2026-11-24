"""One fixed group query and deterministic message identity projection."""

import hashlib
import json
from copy import deepcopy

from modules.chat_history.contracts import ChatHistoryQueryError
from modules.health_analysis.contracts import AnalysisCapacityError
from modules.health_analysis.contracts import SourceUnavailableError


class ChatHistoryMaterialsAdapter:
    def __init__(self, tool, *, patient_sender_reader=None, max_messages=1000):
        if type(max_messages) is not int or max_messages < 1:
            raise ValueError('max_messages must be a positive integer')
        self.tool, self.max_messages = tool, max_messages
        self.patient_sender_reader = patient_sender_reader

    async def read(self, scope):
        try:
            result = await self.tool.handler({'groupId': scope.group_id}, None)
        except ChatHistoryQueryError as error:
            raise SourceUnavailableError('chat_' + error.category) from None
        if (
            not isinstance(result, dict)
            or not isinstance(result.get('chatLogs'), list)
            or type(result.get('chatLogsNum')) is not int
            or result['chatLogsNum'] < 0
        ):
            raise SourceUnavailableError('chat_invalid_response')
        rows = result['chatLogs']
        if len(rows) > self.max_messages:
            raise AnalysisCapacityError('Chat message capacity exceeded')
        sender_ids = scope.patient_sender_ids
        if self.patient_sender_reader is not None:
            sender_ids = await self.patient_sender_reader.read(user_id=scope.user_id, group_id=scope.group_id)
        if not isinstance(sender_ids, (list, tuple)) or any(
            not isinstance(value, str) or not value or value != value.strip() for value in sender_ids
        ):
            raise TypeError('Patient sender reader returned invalid identities')
        patients = frozenset(sender_ids)
        materials, attachments, ignored, seen = [], [], {}, {}
        for position, row in enumerate(rows):
            if (
                not isinstance(row, dict)
                or not isinstance(row.get('chatLog'), dict)
                or type(row.get('isRevoked')) is not bool
            ):
                raise SourceUnavailableError('chat_invalid_response')
            message = row['chatLog']
            if message.get('groupID') is not None and message['groupID'] != scope.group_id:
                raise PermissionError('Chat history escaped the requested group')
            server_id, client_id = message.get('serverMsgID'), message.get('clientMsgID')
            identity = (
                ('server', server_id)
                if isinstance(server_id, str) and server_id
                else ('client', client_id)
                if isinstance(client_id, str) and client_id
                else None
            )
            if identity is None:
                ignored['missing_message_id'] = ignored.get('missing_message_id', 0) + 1
                continue
            previous = seen.get(identity)
            if previous is not None:
                # A revocation wins even if its duplicate occurs before content.
                if row.get('isRevoked') is True:
                    previous['isRevoked'] = True
                if any(
                    message.get(key) != previous['chatLog'].get(key) for key in ('content', 'sendID', 'contentType')
                ):
                    previous['_conflicting'] = True
                continue
            seen[identity] = {**deepcopy(row), '_identity': identity, '_position': position}
        ordered = sorted(
            seen.values(),
            key=lambda row: (
                type(row['chatLog'].get('sendTime')) not in (int, float),
                row['chatLog'].get('sendTime') if type(row['chatLog'].get('sendTime')) in (int, float) else 0,
                row['_position'],
            ),
        )
        for row in ordered:
            message, identity = row['chatLog'], row['_identity']
            reason = None
            if row.get('isRevoked') is True:
                reason = 'revoked'
            elif row.get('_conflicting'):
                reason = 'conflicting_message_identity'
            elif type(message.get('contentType')) is not int or message.get('contentType') not in (101, 102, 105):
                reason = 'unsupported_message_type'
            elif not isinstance(message.get('sendID'), str) or message.get('sendID') not in patients:
                reason = 'unverified_sender'
            content = message.get('content')
            if reason is None and (not isinstance(content, str) or not content.strip()):
                reason = 'empty_content'
            if reason:
                ignored[reason] = ignored.get(reason, 0) + 1
                continue
            metadata = {
                'groupId': scope.group_id,
                'messageId': identity[1],
                'messageIdentityKind': identity[0],
                'serverMsgID': message.get('serverMsgID'),
                'clientMsgID': message.get('clientMsgID'),
                'senderId': message.get('sendID'),
                'senderRole': 'patient',
                'subject': 'self_reported_subject_unverified',
                'statementType': 'patient_message_not_verified_fact',
                'sentAtMilliseconds': message.get('sendTime'),
                'isRevoked': False,
                'contentType': message['contentType'],
            }
            if message['contentType'] in (102, 105):
                attachments.append({'content': content, 'metadata': metadata})
                continue
            material_id = hashlib.sha256(
                json.dumps([scope.group_id, identity], ensure_ascii=False).encode('utf-8')
            ).hexdigest()
            materials.append({'id': 'chat:' + material_id, 'kind': 'chat', 'content': content, 'metadata': metadata})
        coverage = {
            'upstreamSingleResponse': True,
            'completeHistory': False,
            'upstreamReportedCount': result.get('chatLogsNum'),
            'receivedMessages': len(rows),
            'uniqueMessages': len(seen),
            'adoptedTextMessages': len(materials),
            'ignored': ignored,
            'senderMappingAvailable': bool(patients),
            'patientSenderIds': sorted(patients),
            'attachmentHints': attachments,
        }
        return {
            'materials': materials,
            'status': 'available' if materials else 'empty',
            'coverage': coverage,
            'sourceSnapshot': {'messages': deepcopy(materials), 'coverage': deepcopy(coverage)},
        }
