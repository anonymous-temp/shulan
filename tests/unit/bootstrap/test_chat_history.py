"""The optional chat query factory is configured only when explicitly requested."""

import builtins
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from bootstrap.chat_history import create_application_chat_history_tool
from modules.chat_history import ChatHistoryQueryError


def test_public_modules_and_explicit_factories_do_not_load_configuration_or_construct_http_clients():
    script = """
import importlib
import sys

import httpx

class ConfigurationGuard:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "config" or fullname.startswith("config."):
            raise AssertionError("chat history import must not load deployment configuration")

def forbid_client(*args, **kwargs):
    raise AssertionError("chat history import must not create an HTTP client")

sys.meta_path.insert(0, ConfigurationGuard())
httpx.AsyncClient.__init__ = forbid_client
httpx.Client.__init__ = forbid_client
for name in ("modules.chat_history", "adapters.chat_history", "bootstrap.chat_history"):
    importlib.import_module(name)
from bootstrap.chat_history import create_application_chat_history_tool
from modules.chat_history import build_chat_history_tool

configured = create_application_chat_history_tool(query_url="https://unit-test.invalid/api/openim/open-api/message/search")
assert configured.name == "query_chat_history"

class Reader:
    async def query(self, group_id):
        return {"chatLogs": [], "chatLogsNum": 0}

assert build_chat_history_tool(Reader()).name == "query_chat_history"
assert "config.env" not in sys.modules
"""
    subprocess.run(
        [sys.executable, '-B', '-c', script],
        cwd=Path(__file__).resolve().parents[3],
        check=True,
        capture_output=True,
        text=True,
    )


def test_lazy_bootstrap_exports_remain_compatible():
    import bootstrap
    from bootstrap.container import AppContainer
    from bootstrap.factory import build_application_bundle
    from bootstrap.factory import build_container
    from bootstrap.factory import create_application_health_report_interpreter

    expected = {
        'AppContainer': AppContainer,
        'build_application_bundle': build_application_bundle,
        'build_container': build_container,
        'create_application_health_report_interpreter': create_application_health_report_interpreter,
    }
    assert set(expected) <= set(bootstrap.__all__)
    assert set(expected) <= set(dir(bootstrap))
    for name, value in expected.items():
        assert getattr(bootstrap, name) is value


def test_lazy_agent_runtime_exports_remain_compatible():
    import agents
    from agents.runtime.container import AgentRuntime
    from agents.runtime.lifecycle import AgentRuntimeSettings
    from agents.runtime.lifecycle import create_agent_runtime

    expected = {
        'AgentRuntime': AgentRuntime,
        'AgentRuntimeSettings': AgentRuntimeSettings,
        'create_agent_runtime': create_agent_runtime,
    }
    assert set(expected) <= set(agents.__all__)
    assert set(expected) <= set(dir(agents))
    for name, value in expected.items():
        assert getattr(agents, name) is value


def fake_configuration(monkeypatch, query_url):
    reads = []
    env = ModuleType('config.env')

    def read(name):
        if name != 'CHAT_HISTORY_QUERY_URL':
            raise AttributeError(name)
        reads.append(name)
        return query_url

    env.__getattr__ = read
    config = ModuleType('config')
    config.env = env
    monkeypatch.setitem(sys.modules, 'config', config)
    monkeypatch.setitem(sys.modules, 'config.env', env)
    return reads


@pytest.mark.asyncio
async def test_application_factory_reads_configured_url_only_when_called(monkeypatch):
    query_url = 'https://test-chat-history.invalid/api/openim/open-api/message/search?route=test'
    reads = fake_configuration(monkeypatch, query_url)
    calls = []
    clients = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200, json={'state': 200, 'results': {'errCode': 0, 'data': {'chatLogs': [], 'chatLogsNum': 0}}}
        )

    def factory(**options):
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond), **options)
        clients.append(client)
        return client

    assert reads == []
    tool = create_application_chat_history_tool(client_factory=factory)
    assert reads
    assert calls == []
    assert clients == []
    assert await tool.handler({'groupId': 'test-group'}, None) == {'chatLogs': [], 'chatLogsNum': 0}
    assert str(calls[0].url) == query_url
    assert json.loads(calls[0].content) == {'groupId': 'test-group'}
    assert clients[0].is_closed


def test_explicit_url_factory_does_not_import_configuration(monkeypatch):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == 'config' or name.startswith('config.'):
            raise AssertionError('An explicitly configured factory must not read deployment configuration')
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', guarded)
    tool = create_application_chat_history_tool(query_url='https://test-chat-history.invalid/search')
    assert tool.name == 'query_chat_history'


@pytest.mark.parametrize('explicit', [False, True])
def test_unconfigured_factory_raises_without_an_http_client(monkeypatch, explicit):
    fake_configuration(monkeypatch, '')

    def forbid_client(**options):
        pytest.fail('Empty configuration must not construct an HTTP client')

    arguments = {'client_factory': forbid_client}
    if explicit:
        arguments['query_url'] = ''
    with pytest.raises(ChatHistoryQueryError) as caught:
        create_application_chat_history_tool(**arguments)
    assert caught.value.category == 'not_configured'
