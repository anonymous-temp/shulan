"""Validate callback deployment settings without loading a real .env file."""

import runpy
from pathlib import Path
from unittest.mock import patch

import pytest
from dotenv import dotenv_values
from dotenv import load_dotenv
from loongspears.utils.env import EnvConfigError

ROOT = Path(__file__).resolve().parents[3]
DEFAULTS = {
    'REPORT_CALLBACK_RETRY_DELAYS_SECONDS': (30, 120, 600, 3600),
    'REPORT_CALLBACK_POLL_INTERVAL_SECONDS': 5,
    'REPORT_CALLBACK_CONCURRENCY': 4,
    'REPORT_CALLBACK_TIMEOUT_SECONDS': 15,
    'REPORT_CALLBACK_BATCH_SIZE': 100,
    'REPORT_CALLBACK_RETRY_JITTER_RATIO': 0.1,
    'REPORT_CALLBACK_BACKLOG_LOG_INTERVAL_SECONDS': 300,
}


@pytest.fixture
def read_settings(monkeypatch):
    for name in DEFAULTS:
        monkeypatch.delenv(f'SHULAN_{name}', raising=False)
    for name in ('GUT_BARRIER_PRODUCT_URL', 'SHULAN_GUT_BARRIER_PRODUCT_URL'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('SHULAN_ENVIRONMENT', 'test')
    monkeypatch.setenv('SHULAN_REPORT_CALLBACK_URL', 'https://unit-test.invalid/callback')
    monkeypatch.setenv('SHULAN_REPORT_PRODUCT_URL', 'https://unit-test.invalid/products')

    def read(**values):
        for name, value in values.items():
            monkeypatch.setenv(f'SHULAN_{name}', value)
        with patch('loongspears.utils.env.load_env', return_value=None):
            return runpy.run_path(str(ROOT / 'config/env.py'))

    return read


@pytest.mark.parametrize('empty', [False, True])
def test_callback_defaults_preserve_existing_behavior(read_settings, empty):
    settings = read_settings(**({name: '' for name in DEFAULTS} if empty else {}))
    assert {name: settings[name] for name in DEFAULTS} == DEFAULTS


def test_callback_settings_accept_custom_schedule_and_disabled_jitter(read_settings):
    settings = read_settings(
        REPORT_CALLBACK_RETRY_DELAYS_SECONDS=' 5, 10, 30 ',
        REPORT_CALLBACK_POLL_INTERVAL_SECONDS='1',
        REPORT_CALLBACK_CONCURRENCY='8',
        REPORT_CALLBACK_TIMEOUT_SECONDS='45',
        REPORT_CALLBACK_BATCH_SIZE='7',
        REPORT_CALLBACK_RETRY_JITTER_RATIO='0',
        REPORT_CALLBACK_BACKLOG_LOG_INTERVAL_SECONDS='60',
    )
    assert {name: settings[name] for name in DEFAULTS} == {
        'REPORT_CALLBACK_RETRY_DELAYS_SECONDS': (5, 10, 30),
        'REPORT_CALLBACK_POLL_INTERVAL_SECONDS': 1,
        'REPORT_CALLBACK_CONCURRENCY': 8,
        'REPORT_CALLBACK_TIMEOUT_SECONDS': 45,
        'REPORT_CALLBACK_BATCH_SIZE': 7,
        'REPORT_CALLBACK_RETRY_JITTER_RATIO': 0,
        'REPORT_CALLBACK_BACKLOG_LOG_INTERVAL_SECONDS': 60,
    }
    assert read_settings(REPORT_CALLBACK_RETRY_DELAYS_SECONDS='600')['REPORT_CALLBACK_RETRY_DELAYS_SECONDS'] == (600,)


@pytest.mark.parametrize('raw', ['0', '-1', '10,5', '1,,2', '1.5', '[30,120]', '31536001'])
def test_invalid_retry_schedule_fails_at_startup(read_settings, raw):
    with pytest.raises(EnvConfigError, match='SHULAN_REPORT_CALLBACK_RETRY_DELAYS_SECONDS'):
        read_settings(REPORT_CALLBACK_RETRY_DELAYS_SECONDS=raw)


@pytest.mark.parametrize(
    'name, raw',
    [
        ('POLL_INTERVAL_SECONDS', '0'),
        ('POLL_INTERVAL_SECONDS', '3601'),
        ('CONCURRENCY', '0'),
        ('CONCURRENCY', '65'),
        ('TIMEOUT_SECONDS', '0'),
        ('TIMEOUT_SECONDS', '61'),
        ('BATCH_SIZE', '0'),
        ('BATCH_SIZE', '10001'),
        ('RETRY_JITTER_RATIO', '-0.1'),
        ('RETRY_JITTER_RATIO', '1'),
        ('RETRY_JITTER_RATIO', 'nan'),
        ('RETRY_JITTER_RATIO', 'inf'),
        ('BACKLOG_LOG_INTERVAL_SECONDS', '0'),
        ('BACKLOG_LOG_INTERVAL_SECONDS', '86401'),
    ],
)
def test_invalid_callback_limits_fail_at_startup(read_settings, name, raw):
    with pytest.raises(EnvConfigError, match=f'SHULAN_REPORT_CALLBACK_{name}'):
        read_settings(**{f'REPORT_CALLBACK_{name}': raw})


@pytest.mark.parametrize(
    'filename',
    [
        '.env.example',
        '.env.example.dev',
        '.env.example.test',
        '.env.example.production',
    ],
)
def test_all_deployment_templates_document_callback_defaults(filename):
    lines = (ROOT / filename).read_text(encoding='utf-8').splitlines()
    for name, value in DEFAULTS.items():
        encoded = ','.join(map(str, value)) if isinstance(value, tuple) else str(value)
        assert f'SHULAN_{name}={encoded}' in lines


SHARED_ENDPOINTS = ('REPORT_CALLBACK_URL', 'REPORT_PRODUCT_URL')


@pytest.mark.parametrize('name', SHARED_ENDPOINTS)
@pytest.mark.parametrize(
    'url',
    [
        'http://m.slmc.top/api/callback',
        'https://test.slmc.top/api/callback',
        'https://TEST.SLMC.TOP./api/callback',
        'https://receiver.test.slmc.top/api/callback',
        'https://localhost/api/callback',
        'https://LOCALHOST./api/callback',
        'https://127.0.0.1:10002/callback',
        'https://127.42.0.7/callback',
        'https://127.1/callback',
        'https://2130706433/callback',
        'https://0177.0.0.1/callback',
        'https://0x7f000001/callback',
        'https://[::1]:10002/callback',
        'https://[::ffff:127.0.0.1]/callback',
    ],
)
def test_production_rejects_test_and_local_report_endpoints_at_startup(read_settings, name, url):
    with pytest.raises(EnvConfigError, match=f'SHULAN_{name}'):
        read_settings(ENVIRONMENT='production', **{name: url})


@pytest.mark.parametrize('name', SHARED_ENDPOINTS)
@pytest.mark.parametrize(
    'url',
    [
        'https://m.slmc.top/api/callback',
        'https://receiver.example.org:8443/callback?tenant=shulan',
        'https://test-other.invalid/callback',
    ],
)
def test_production_accepts_configured_https_endpoints(read_settings, name, url):
    settings = read_settings(ENVIRONMENT='production', **{name: url})
    assert settings[name] == url


@pytest.mark.parametrize('environment', ['dev', 'test'])
@pytest.mark.parametrize(
    'url',
    [
        'https://test.slmc.top/api/basic-health-assessment/ai-full-back/callback',
        'http://127.0.0.1:10002/callback',
        'http://localhost:10002/callback',
    ],
)
def test_nonproduction_allows_explicit_test_or_local_receivers(read_settings, environment, url):
    settings = read_settings(ENVIRONMENT=environment, REPORT_CALLBACK_URL=url, REPORT_PRODUCT_URL=url)
    assert settings['REPORT_CALLBACK_URL'] == settings['REPORT_PRODUCT_URL'] == url


@pytest.mark.parametrize('name', SHARED_ENDPOINTS)
@pytest.mark.parametrize(
    'url',
    [
        '',
        '   ',
        'not-a-url',
        'ftp://receiver.invalid/callback',
        '//receiver.invalid/callback',
        'https:///callback',
        'https://receiver.invalid:invalid/callback',
        'https://receiver.invalid:65536/callback',
        'https://receiver.invalid:0/callback',
        'https://[invalid/callback',
        'https://user:password@receiver.invalid/callback',
        'https://receiver.invalid/callback#fragment',
        'https://receiver.invalid/has space',
        'https://receiver.invalid/has\tcontrol',
        'https://receiver.invalid\\other/callback',
        'https://%74est.slmc.top/callback',
        'https://./callback',
    ],
)
def test_malformed_report_endpoints_fail_during_startup(read_settings, name, url):
    with pytest.raises(EnvConfigError, match=f'SHULAN_{name}'):
        read_settings(**{name: url})


@pytest.mark.parametrize('name', SHARED_ENDPOINTS)
def test_report_endpoints_remain_required_without_a_fallback(read_settings, monkeypatch, name):
    monkeypatch.delenv(f'SHULAN_{name}')
    with pytest.raises(EnvConfigError, match=f'SHULAN_{name}'):
        read_settings()


@pytest.mark.parametrize('name', SHARED_ENDPOINTS)
def test_invalid_endpoint_error_does_not_expose_url_or_query_credentials(read_settings, name):
    url = 'https://test.slmc.top/private/path?token=must-not-appear'
    with pytest.raises(EnvConfigError, match=f'SHULAN_{name}') as caught:
        read_settings(ENVIRONMENT='production', **{name: url})
    assert url not in str(caught.value)
    assert 'must-not-appear' not in str(caught.value)


@pytest.mark.parametrize('prefix', ['', 'SHULAN_'])
def test_legacy_product_override_cannot_bypass_production_validation(read_settings, monkeypatch, prefix):
    monkeypatch.setenv(f'{prefix}GUT_BARRIER_PRODUCT_URL', 'https://test.slmc.top/catalog?token=private')
    with pytest.raises(EnvConfigError, match='GUT_BARRIER_PRODUCT_URL') as caught:
        read_settings(ENVIRONMENT='production')
    assert 'token=private' not in str(caught.value)


@pytest.mark.parametrize('prefix', ['', 'SHULAN_'])
def test_explicit_empty_product_override_still_disables_optional_gut_catalog(read_settings, monkeypatch, prefix):
    monkeypatch.setenv(f'{prefix}GUT_BARRIER_PRODUCT_URL', '')
    settings = read_settings(ENVIRONMENT='production')
    assert settings['GUT_BARRIER_PRODUCT_URL'] == ''
    assert settings['REPORT_PRODUCT_URL'] == 'https://unit-test.invalid/products'


def test_canonical_empty_product_override_takes_precedence_over_unsafe_legacy_value(read_settings, monkeypatch):
    monkeypatch.setenv('GUT_BARRIER_PRODUCT_URL', 'https://test.slmc.top/catalog')
    settings = read_settings(ENVIRONMENT='production', GUT_BARRIER_PRODUCT_URL='')
    assert settings['GUT_BARRIER_PRODUCT_URL'] == ''


@pytest.mark.parametrize('filename', ['.env.example.dev', '.env.example.test', '.env.example.production'])
def test_deployment_templates_load_appropriate_shared_report_endpoints(read_settings, filename):
    values = dotenv_values(ROOT / filename, interpolate=False)
    names = ('ENVIRONMENT', *SHARED_ENDPOINTS)
    settings = read_settings(**{name: values[f'SHULAN_{name}'] for name in names})
    for name in SHARED_ENDPOINTS:
        assert settings[name] == values[f'SHULAN_{name}']
    assert settings['GUT_BARRIER_PRODUCT_URL'] == settings['REPORT_PRODUCT_URL']


def test_system_report_endpoints_override_dotenv_without_reading_deployment_file(read_settings, monkeypatch, tmp_path):
    env_file = tmp_path / '.env'
    env_file.write_text(
        'SHULAN_ENVIRONMENT=production\n'
        'SHULAN_REPORT_CALLBACK_URL=https://test.slmc.top/callback\n'
        'SHULAN_REPORT_PRODUCT_URL=https://test.slmc.top/catalog\n',
        encoding='utf-8',
    )
    monkeypatch.setenv('SHULAN_ENVIRONMENT', 'production')
    monkeypatch.setenv('SHULAN_REPORT_CALLBACK_URL', 'https://m.slmc.top/callback')
    monkeypatch.setenv('SHULAN_REPORT_PRODUCT_URL', 'https://m.slmc.top/catalog')

    def load_test_file(path, *, override, required):
        assert path == ROOT / '.env' and override is False and required is False
        return load_dotenv(env_file, override=override)

    with patch('loongspears.utils.env.load_env', side_effect=load_test_file) as load:
        settings = runpy.run_path(str(ROOT / 'config/env.py'))
    load.assert_called_once()
    assert settings['REPORT_CALLBACK_URL'] == 'https://m.slmc.top/callback'
    assert settings['REPORT_PRODUCT_URL'] == 'https://m.slmc.top/catalog'
    assert settings['GUT_BARRIER_PRODUCT_URL'] == 'https://m.slmc.top/catalog'


def test_endpoint_validator_reads_current_environment_when_not_explicit(monkeypatch):
    from config import env
    from config.report_endpoints import validate_report_endpoint

    monkeypatch.setattr(env, 'ENVIRONMENT', 'production')
    with pytest.raises(ValueError, match='REPORT_CALLBACK_URL'):
        validate_report_endpoint('https://test.slmc.top/callback')
    monkeypatch.setattr(env, 'ENVIRONMENT', 'test')
    assert validate_report_endpoint('http://127.0.0.1:10002/callback') == 'http://127.0.0.1:10002/callback'
