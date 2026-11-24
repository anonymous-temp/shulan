"""LangGraph MongoDB checkpointer 的项目内构造边界。"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.mongodb import MongoDBSaver


def create_mongodb_checkpointer(
        client: Any,
        *,
        database_name: str,
        checkpoint_collection_name: str,
        writes_collection_name: str,
) -> BaseCheckpointSaver:
    """使用独立 collection 创建官方 MongoDBSaver。"""
    return MongoDBSaver(
        client,
        db_name=database_name,
        checkpoint_collection_name=checkpoint_collection_name,
        writes_collection_name=writes_collection_name,
    )
