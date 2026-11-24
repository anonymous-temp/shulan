"""Fresh interpreters verify import isolation and the compatible public facade."""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def run_isolated(source, *args):
    result = subprocess.run(
        [sys.executable, '-c', textwrap.dedent(source), *map(str, args)],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('order', [
    'preview,context,files', 'context,files,preview', 'files,preview,context',
])
def test_helper_imports_do_not_load_configuration_or_start_threads(order):
    run_isolated('''
        import importlib
        import importlib.abc
        import sys
        import threading

        class RejectConfiguration(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == 'config' or fullname.startswith(('config.', 'loongspears')):
                    raise AssertionError('Helper imported configuration: ' + fullname)

        sys.meta_path.insert(0, RejectConfiguration())
        before = set(threading.enumerate())
        import utils.log
        for name in sys.argv[1].split(','):
            importlib.import_module('utils.log.' + name)
        from utils.log.context import bind_log_context, log_context
        from utils.log.preview import PreviewLimits, preview
        with bind_log_context(user_id='u'):
            assert log_context().user_id == 'u'
        assert log_context().user_id is None
        assert preview({'x': 'abcdefgh'}, PreviewLimits(string_chars=4)) == '{"x":"ab***gh"}'
        assert 'utils.log.core' not in sys.modules
        assert 'config.env' not in sys.modules
        assert set(threading.enumerate()) == before
        assert 'getLogger' in dir(utils.log)
        assert not hasattr(utils.log, '_writer')
        assert 'utils.log.core' not in sys.modules
    ''', order)


@pytest.mark.parametrize('first', ['public', 'core', 'helpers'])
def test_import_order_shares_one_core_and_writer(first, tmp_path):
    run_isolated('''
        import importlib
        import logging
        import runpy
        import sys
        import threading
        from pathlib import Path

        # Use the suite's offline credentials and disabled dotenv loader.
        runpy.run_path('tests/unit/conftest.py')
        from config import env
        env.LOG_FILE_ENABLED = True
        env.LOG_LEVEL = 'INFO'
        env.LOG_DIR = Path(sys.argv[2]) / 'logs'
        first = sys.argv[1]
        if first == 'helpers':
            import utils.log.context
            import utils.log.preview
            assert 'utils.log.core' not in sys.modules
        elif first == 'core':
            import utils.log.core
        else:
            from utils.log import getLogger

        from utils import log
        from utils.log import core
        from utils.log import getLogger, global_logger, ContextFilter
        from utils.log import configure_tornado_logging, shutdown_logging
        from utils.log.context import bind_log_context
        assert getLogger is core.getLogger
        assert global_logger is core.global_logger is getLogger('global')
        assert ContextFilter is core.ContextFilter
        assert configure_tornado_logging is core.configure_tornado_logging
        assert shutdown_logging is core.shutdown_logging
        assert not hasattr(log, '_writer')
        assert all(name in dir(log) for name in log.__all__)

        logger = getLogger('package-compatibility')
        for _ in range(3):
            assert getLogger('package-compatibility') is logger
            configure_tornado_logging()
        assert len(logger.handlers) == 2
        assert sum(t.name == 'log-files' for t in threading.enumerate()) == 1
        writer = core._writer
        with bind_log_context(user_id='u'):
            logger.info('one user record')
        logging.getLogger('tornado.general').info('one system record')
        shutdown_logging()
        shutdown_logging()
        assert core._writer is None and not writer._thread.is_alive()
        root = env.LOG_DIR
        all_text = (root / 'all/all.log').read_text(encoding='utf-8')
        assert all_text.count('one user record') == 1
        assert all_text.count('one system record') == 1
        assert 'one user record' in (root / 'user/u/user.log').read_text(encoding='utf-8')
        assert 'one system record' in (root / 'system/system.log').read_text(encoding='utf-8')
    ''', first, tmp_path)
