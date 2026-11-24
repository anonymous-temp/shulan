"""Bounded periodic source work with process-local overlap prevention and seek continuation."""


class SourceRecoveryRunner:
    def __init__(self, recover):
        self.recover = recover
        self.running = False
        self.accepting = True

    def stop_accepting(self):
        self.accepting = False

    async def recover_pending(self):
        if not self.accepting or self.running:
            return None
        self.running = True
        try:
            return await self.recover()
        finally:
            self.running = False


class DocumentOutboxRecovery:
    """One bounded page per tick; restart rescans durable state, never loses intents."""

    def __init__(self, recovery):
        self.recovery = recovery
        self.source_cursor = None
        self.intent_cursor = None

    async def recover_pending(self):
        result = await self.recovery.recover_pending(
            after_source_id=self.source_cursor,
            after_intent_id=self.intent_cursor,
        )
        self.source_cursor, self.intent_cursor = result.next_source_id, result.next_intent_id
        return result
