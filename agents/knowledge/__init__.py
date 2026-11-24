"""Shulan Agent 知识检索公共入口。"""

from .evimed_gateway import EviMedKnowledgeGateway
from .errors import KnowledgeRetrievalError
from .profile import KNOWLEDGE_ROUTE_PROFILE
from .profile import create_knowledge_route_profile
from .profile import ensure_knowledge_route_profile
from .protocol import DisabledKnowledgeGateway
from .protocol import KnowledgeGateway
from .routing import format_knowledge_context
from .routing import parse_retrieval_plan
from .routing import plan_knowledge_retrieval
from .routing import retrieve_knowledge
from .types import KnowledgeDocument
from .types import KnowledgeQuery
from .types import KnowledgeRetrievalPlan
from .types import KnowledgeSource

__all__ = [
    'DisabledKnowledgeGateway',
    'EviMedKnowledgeGateway',
    'KNOWLEDGE_ROUTE_PROFILE',
    'KnowledgeDocument',
    'KnowledgeGateway',
    'KnowledgeQuery',
    'KnowledgeRetrievalError',
    'KnowledgeRetrievalPlan',
    'KnowledgeSource',
    'create_knowledge_route_profile',
    'ensure_knowledge_route_profile',
    'format_knowledge_context',
    'parse_retrieval_plan',
    'plan_knowledge_retrieval',
    'retrieve_knowledge',
]
