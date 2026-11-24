"""Live Record dialogue acceptance using synthetic patients and memory storage."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.unit._support.harnesses import record_dialogue_acceptance as harness
from tests.unit._support.harnesses.record_dialogue_acceptance import SmokeGateway


def build_delegate(settings, clients):
    if not settings:
        return SmokeGateway()
    import aion

    from agents.llm.aion_gateway import AionLLMGateway

    def factory(*args, **kwargs):
        client = aion.create_chat_client(*args, **kwargs)
        clients.append(client)
        return client

    return AionLLMGateway(
        provider=settings['SHULAN_LLM_PROVIDER'],
        api_key=settings['SHULAN_LLM_API_KEY'],
        default_model=settings['SHULAN_LLM_DEFAULT_MODEL'],
        client_factory=factory,
    )



def main(argv=None):
    return harness.main(argv, online=True, delegate_factory=build_delegate)


if __name__ == '__main__':
    raise SystemExit(main())
