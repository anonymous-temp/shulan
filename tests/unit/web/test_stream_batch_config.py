"""SSE settings load centrally without opening deployment credential files."""

import runpy
from pathlib import Path
from unittest.mock import patch

import pytest
from dotenv import load_dotenv
from loongspears.utils.env import EnvConfigError

ROOT = Path(__file__).resolve().parents[3]
DEFAULTS = {'SSE_TEXT_BATCH_CHARS': 512, 'SSE_TEXT_BATCH_SECONDS': 0.05}


@pytest.fixture
def read_settings(monkeypatch):
    for name in DEFAULTS:
        monkeypatch.delenv(f'SHULAN_{name}', raising=False)

    def read(**values):
        for name, value in values.items():
            monkeypatch.setenv(f'SHULAN_{name}', value)
        with patch('loongspears.utils.env.load_env', return_value=None):
            return runpy.run_path(str(ROOT / 'config/env.py'))

    return read


@pytest.mark.parametrize('empty', [False, True])
def test_unset_or_empty_settings_preserve_defaults(read_settings, empty):
    settings = read_settings(**({name: '' for name in DEFAULTS} if empty else {}))
    assert {name: settings[name] for name in DEFAULTS} == DEFAULTS


def test_custom_values_and_positive_minimum(read_settings):
    settings = read_settings(SSE_TEXT_BATCH_CHARS='1024', SSE_TEXT_BATCH_SECONDS='0.1')
    assert settings['SSE_TEXT_BATCH_CHARS'] == 1024
    assert settings['SSE_TEXT_BATCH_SECONDS'] == 0.1
    settings = read_settings(SSE_TEXT_BATCH_CHARS='1', SSE_TEXT_BATCH_SECONDS='0.001')
    assert settings['SSE_TEXT_BATCH_CHARS'] == 1
    assert settings['SSE_TEXT_BATCH_SECONDS'] == 0.001


@pytest.mark.parametrize('name, value', [
    ('SSE_TEXT_BATCH_CHARS', '0'), ('SSE_TEXT_BATCH_CHARS', '-1'),
    ('SSE_TEXT_BATCH_CHARS', '1.5'), ('SSE_TEXT_BATCH_CHARS', 'text'),
    ('SSE_TEXT_BATCH_SECONDS', '0'), ('SSE_TEXT_BATCH_SECONDS', '-0.1'),
    ('SSE_TEXT_BATCH_SECONDS', 'text'), ('SSE_TEXT_BATCH_SECONDS', 'nan'),
    ('SSE_TEXT_BATCH_SECONDS', 'inf'), ('SSE_TEXT_BATCH_SECONDS', '-inf'),
])
def test_invalid_settings_fail_at_startup(read_settings, name, value):
    with pytest.raises(EnvConfigError, match=f'SHULAN_{name}'):
        read_settings(**{name: value})


def test_dotenv_values_and_system_environment_precedence(read_settings, monkeypatch, tmp_path):
    # Redirect the existing loader to a temporary file, never the real .env.
    env_file = tmp_path / '.env'
    env_file.write_text('SHULAN_SSE_TEXT_BATCH_CHARS=2048\nSHULAN_SSE_TEXT_BATCH_SECONDS=0.2\n')

    def load_test_file(path, *, override, required):
        assert path == ROOT / '.env' and override is False and required is False
        return load_dotenv(env_file, override=override)

    with patch('loongspears.utils.env.load_env', side_effect=load_test_file):
        # monkeypatch tracks both dotenv's writes and the later explicit override.
        for name in DEFAULTS:
            monkeypatch.setenv(f'SHULAN_{name}', '')
            monkeypatch.delenv(f'SHULAN_{name}')
        settings = runpy.run_path(str(ROOT / 'config/env.py'))
        assert settings['SSE_TEXT_BATCH_CHARS'] == 2048
        assert settings['SSE_TEXT_BATCH_SECONDS'] == 0.2
        monkeypatch.setenv('SHULAN_SSE_TEXT_BATCH_SECONDS', '0.3')
        settings = runpy.run_path(str(ROOT / 'config/env.py'))
        assert settings['SSE_TEXT_BATCH_SECONDS'] == 0.3


@pytest.mark.asyncio
async def test_configured_values_reach_the_shared_sender(read_settings):
    settings = read_settings(SSE_TEXT_BATCH_CHARS='4', SSE_TEXT_BATCH_SECONDS='0.123')
    with patch.multiple('config.env', **{name: settings[name] for name in DEFAULTS}):
        sender = runpy.run_path(str(ROOT / 'web/stream_batch.py'))['send_batched_sse']
    assert sender.__kwdefaults__['max_chars'] == 4
    assert sender.__kwdefaults__['max_delay'] == 0.123
    received = []

    async def source():
        for text in ('首', 'ab', 'cd', '尾'):
            yield {'data': {'type': 'content', 'content': text}}

    async def send(payload):
        received.append(payload['data']['content'])

    await sender(source(), send, disconnected=lambda: False)
    assert received == ['首', 'abcd', '尾']


@pytest.mark.parametrize('filename', [
    '.env.example', '.env.example.dev', '.env.example.test', '.env.example.production',
])
def test_all_templates_document_defaults(filename):
    lines = (ROOT / filename).read_text(encoding='utf-8').splitlines()
    for name, value in DEFAULTS.items():
        assert f'SHULAN_{name}={value}' in lines
