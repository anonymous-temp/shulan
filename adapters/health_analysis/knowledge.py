"""Translate actual knowledge transports into the domain's failure boundary."""

import httpx

from agents.knowledge.errors import KnowledgeRetrievalError
from modules.health_analysis.contracts import AnalysisTimeoutError
from modules.health_analysis.contracts import KnowledgeUnavailableError


class HealthAnalysisKnowledge:
    def __init__(self, gateway):
        self.gateway = gateway

    @property
    def enabled(self):
        return self.gateway is not None and self.gateway.enabled

    async def retrieve(self, query):
        from utils.knowledge import KnowledgeAPIError

        try:
            return await self.gateway.retrieve(query)
        except AnalysisTimeoutError:
            raise
        except (KnowledgeRetrievalError, KnowledgeAPIError, httpx.HTTPError, TimeoutError, ConnectionError) as error:
            raise KnowledgeUnavailableError('Knowledge dependency unavailable') from error
