"""Offline unit tests must never load deployment credentials from .env."""

from unittest.mock import patch

import pytest

_environment = patch.dict(
    'os.environ',
    {
        'SHULAN_ENVIRONMENT': 'test',
        'SHULAN_LOG_LEVEL': 'CRITICAL',
        'SHULAN_LOG_FILE_ENABLED': 'false',
        'SHULAN_LLM_API_KEY': 'unused-unit-test-key',
        'SHULAN_LLM_PROVIDER': '',
        'SHULAN_LLM_DEFAULT_MODEL': 'unused-unit-test-model',
        'SHULAN_ALIYUN_DOCMIND_ACCESSKEY_ID': 'unused-unit-test-id',
        'SHULAN_ALIYUN_DOCMIND_ACCESSKEY_SECRET': 'unused-unit-test-secret',
        'SHULAN_ALIYUN_DOCMIND_ENDPOINT': 'https://unit-test.invalid',
        'SHULAN_MONGODB_URI': 'mongodb://127.0.0.1:1/?serverSelectionTimeoutMS=10',
        'SHULAN_MONGODB_DATABASE': 'test-shulan',
        'SHULAN_MONGODB_COLL_SESSIONS': 'test-sessions',
        'SHULAN_MONGODB_COLL_HEALTH_REPORTS': 'test-health-reports',
        'SHULAN_REDIS_HOST': '127.0.0.1',
        'SHULAN_REDIS_PORT': '1',
        'SHULAN_REDIS_PASSWORD': '',
        'SHULAN_KNOWLEDGE_API_KEY': '',
        'SHULAN_EVIMED_API_KEY': '',
        'SHULAN_REPORT_CALLBACK_URL': 'https://unit-test.invalid/callback',
        'SHULAN_REPORT_PRODUCT_URL': 'https://unit-test.invalid/products',
    },
)
_dotenv = patch('loongspears.utils.env.load_env', return_value=None)
_environment.start()
_dotenv.start()


def pytest_unconfigure(config):
    _dotenv.stop()
    _environment.stop()


@pytest.fixture
def symlink_or_skip():
    """Skip only when Windows denies the privilege to create a real symlink."""
    def create(link, target, *, target_is_directory=False):
        try:
            link.symlink_to(target, target_is_directory=target_is_directory)
        except OSError as error:
            if getattr(error, 'winerror', None) != 1314:
                raise
            pytest.skip('Symbolic link creation requires Windows privilege (WinError 1314)')
        return link

    return create
