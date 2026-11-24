"""Pure stable identifiers shared across application modules."""


def create_session_lease_id(user_id: str, session_id: str) -> str:
    """构造不易因简单字符串拼接产生碰撞的会话 lease 标识。"""
    return f'business-session:{len(user_id)}:{user_id}:{session_id}'
