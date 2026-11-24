"""Agent 知识检索的领域异常。"""


class KnowledgeRetrievalError(RuntimeError):
    """至少一个检索通道失败，且本轮没有可用资料。"""

    def __init__(self, failure_count: int, query_count: int) -> None:
        super().__init__(
            f'{failure_count} of {query_count} knowledge queries failed',
        )
        self.failure_count = failure_count
        self.query_count = query_count
