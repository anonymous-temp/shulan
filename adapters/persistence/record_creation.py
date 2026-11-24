"""Single-key CAS for expiring QA creation; bounded SCAN also supports Redis Cluster."""

from __future__ import annotations

import json

from adapters.persistence.sessions import _session_cache_key

_SAVE = """
local raw = redis.call('GET', KEYS[1])
if ARGV[1] == '' then
    if raw then return 0 end
else
    if not raw then return 0 end
    local existing = cjson.decode(raw)
    if not existing.qaCreation or existing.qaCreation.revision ~= ARGV[1] then return 0 end
    if existing.qaCreation.expiresAt <= tonumber(redis.call('TIME')[1]) then return 0 end
end
redis.call('SETEX', KEYS[1], ARGV[3], ARGV[2])
return 1
"""


class RedisRecordCreationStore:
    def __init__(self, client):
        self._client = client
        self._scan = None

    async def compare_save(self, user_id, session_id, expected_revision, session, *, ttl_seconds):
        return bool(
            await self._client.eval(
                _SAVE,
                1,
                _session_cache_key(user_id, session_id),
                expected_revision or '',
                json.dumps(session, ensure_ascii=False),
                ttl_seconds,
            )
        )

    async def pending(self, *, now, limit):
        # No permanent queue or cross-slot multi-key transaction. Keep the cursor
        # between ticks so busy records cannot starve later sessions.
        if self._scan is None:
            self._scan = self._client.scan_iter(match='shulan:session:*', count=100).__aiter__()
        pending = []
        try:
            for _ in range(limit * 5):
                try:
                    key = await anext(self._scan)
                except StopAsyncIteration:
                    self._scan = None
                    break
                raw = await self._client.get(key)
                if not raw:
                    continue
                try:
                    session = json.loads(raw)
                    if not isinstance(session, dict) or session.get('type') != 'qa':
                        continue
                    creation = session.get('qaCreation')
                    if (
                        not isinstance(creation, dict)
                        or creation['expiresAt'] <= now
                        or creation['nextAttemptAt'] > now
                    ):
                        continue
                    identity = (session['userId'], session['sessionId'])
                    if not all(isinstance(value, str) and value.strip() for value in identity):
                        continue
                    error = creation.get('error') or {}
                    retryable = isinstance(error, dict) and error.get('retryable') is True
                    if session.get('status') == 'creating' or (session.get('status') == 'create_failed' and retryable):
                        pending.append(identity)
                        if len(pending) >= limit:
                            break
                except (ValueError, TypeError, KeyError):
                    # A malformed or legacy cache entry cannot block other users.
                    continue
        except BaseException:
            self._scan = None
            raise
        return pending


def create_application_record_creation_store():
    from db.redis import client

    return RedisRecordCreationStore(client)


# Compare decoded JSON because existing session writers do not use canonical
# serialization. The comparison and mutation share one Redis key/atomic script,
# including on Redis Cluster; no lease check/write TOCTOU is introduced here.
_SESSION_CAS = """
local function equal(a, b)
    if type(a) ~= type(b) then return false end
    if type(a) ~= 'table' then return a == b end
    for key, value in pairs(a) do
        if not equal(value, b[key]) then return false end
    end
    for key, _ in pairs(b) do if a[key] == nil then return false end end
    return true
end
local raw = redis.call('GET', KEYS[1])
if ARGV[1] == '' then
    if raw then return 0 end
else
    if not raw then return 0 end
    local ok, current = pcall(cjson.decode, raw)
    if not ok or not equal(current, cjson.decode(ARGV[1])) then return 0 end
end
if ARGV[2] == '' then redis.call('DEL', KEYS[1])
else redis.call('SETEX', KEYS[1], ARGV[3], ARGV[2]) end
return 1
"""


class RedisRecordReplyRecoveryStore:
    @staticmethod
    def encode_snapshot(snapshot):
        from bson import json_util

        return json_util.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

    @staticmethod
    def decode_snapshot(value):
        from bson import json_util

        return json_util.loads(value)

    def __init__(self, client):
        self._client = client
        self._scan = None

    async def compare_session(self, user_id, session_id, expected, session, *, ttl_seconds):
        return bool(
            await self._client.eval(
                _SESSION_CAS,
                1,
                _session_cache_key(user_id, session_id),
                '' if expected is None else json.dumps(expected, ensure_ascii=False),
                json.dumps(session, ensure_ascii=False),
                ttl_seconds,
            )
        )

    async def compare_delete(self, user_id, session_id, expected):
        return bool(
            await self._client.eval(
                _SESSION_CAS,
                1,
                _session_cache_key(user_id, session_id),
                json.dumps(expected, ensure_ascii=False),
                '',
                1,
            )
        )

    async def pending_replies(self, *, now, limit):
        from modules.record.application.recovery import eligible_for_scan

        if self._scan is None:
            self._scan = self._client.scan_iter(match='shulan:session:*', count=100).__aiter__()
        found = []
        try:
            for _ in range(limit * 5):
                try:
                    key = await anext(self._scan)
                except StopAsyncIteration:
                    self._scan = None
                    break
                raw = await self._client.get(key)
                if not raw:
                    continue
                try:
                    session = json.loads(raw)
                    if not eligible_for_scan(session, now):
                        continue
                    identity = (session['userId'], session['sessionId'])
                    if all(isinstance(v, str) and v.strip() for v in identity):
                        found.append(identity)
                    if len(found) >= limit:
                        break
                except (ValueError, TypeError, KeyError):
                    continue
        except BaseException:
            self._scan = None
            raise
        return found


def create_application_record_reply_recovery_store():
    from db.redis import client

    return RedisRecordReplyRecoveryStore(client)
