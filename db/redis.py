from database import redis

from config.env import REDIS_DB
from config.env import REDIS_HOST
from config.env import REDIS_MODE
from config.env import REDIS_PASSWORD
from config.env import REDIS_PORT
from config.env import REDIS_PROTOCOL_VERSION

__all__ = [
    'client',
]

connect_info_dict = {
    'host': REDIS_HOST,
    'port': REDIS_PORT,
    'password': REDIS_PASSWORD or None,
    'protocol': REDIS_PROTOCOL_VERSION,
}
# 集群模式不支持选择 db
if REDIS_MODE != 'cluster':
    connect_info_dict['db'] = REDIS_DB

client = redis.Client.connect(mode=REDIS_MODE, is_async=True, **connect_info_dict)
