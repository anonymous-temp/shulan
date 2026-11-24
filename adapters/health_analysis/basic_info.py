"""Translate the public Profile confirmation contract at the consumer boundary."""

from pymongo.errors import AutoReconnect
from pymongo.errors import WriteConcernError

from modules.health_analysis.basic_info_sync import CONFIRMATION_OPERATION_PREFIX
from modules.health_analysis.basic_info_sync import BasicInfoConflictError
from modules.health_analysis.basic_info_sync import BasicInfoUnavailableError
from modules.health_profile.application import BasicInfoConflict
from modules.health_profile.application import BasicInfoUnavailable


class ProfileBasicInfoConfirmationAdapter:
    def __init__(self, provider):
        self.provider = provider

    @staticmethod
    async def _request(operation, *args, **kwargs):
        try:
            result = await operation(*args, **kwargs)
        except BasicInfoConflict:
            raise BasicInfoConflictError('Profile rejected the fixed basic information command') from None
        except (BasicInfoUnavailable, AutoReconnect, WriteConcernError, OSError):
            raise BasicInfoUnavailableError('Profile confirmation storage is temporarily unavailable') from None
        return None if result is None else result.as_dict()

    async def register(self, user_id, operation_id, *, values, confirmed_at, subject_id=None):
        return await self._request(
            self.provider.register,
            user_id,
            operation_id,
            values=values,
            confirmed_at=confirmed_at,
            subject_id=subject_id,
        )

    async def apply(self, user_id, command_id):
        return await self._request(self.provider.apply, user_id, command_id)

    async def get(self, user_id, operation_id):
        return await self._request(self.provider.get, user_id, operation_id)

    async def list_pending(self, *, user_id=None, limit=100, cursor=None):
        return await self._request(
            self.provider.list_pending,
            user_id=user_id,
            limit=limit,
            cursor=cursor,
            operation_prefix=CONFIRMATION_OPERATION_PREFIX,
        )
