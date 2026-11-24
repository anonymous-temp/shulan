"""Real temporary files exercise routing, rollover, restart and write failures."""
import logging
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from utils.log import ContextFilter
from utils.log.context import bind_log_context
from utils.log.files import FileLogConfig
from utils.log.files import FileLogHandler
from utils.log.files import FileLogWriter


@pytest.fixture
def writer(tmp_path):
    value = FileLogWriter(FileLogConfig(tmp_path, user_max_open_files=2))
    yield value
    value.close()


def read(path):
    return path.read_text(encoding='utf-8')


def test_route_and_close_drains(writer, tmp_path):
    writer.submit(None, '系统启动')
    writer.submit('alice', '用户 A 请求')
    writer.submit('bob', '用户 B 请求')
    writer.close()
    assert read(tmp_path / 'all/all.log').splitlines() == ['系统启动', '用户 A 请求', '用户 B 请求']
    assert read(tmp_path / 'system/system.log') == '系统启动\n'
    assert read(tmp_path / 'user/alice/user.log') == '用户 A 请求\n'
    assert read(tmp_path / 'user/bob/user.log') == '用户 B 请求\n'
    assert not writer._thread.is_alive()


def test_format_and_context_frozen_before_queue(writer, tmp_path):
    handler = FileLogHandler(writer)
    handler.addFilter(ContextFilter())
    handler.setFormatter(logging.Formatter('%(user_id)s %(request_uuid)s %(message)s'))
    record = logging.LogRecord('test', logging.INFO, __file__, 1, '%s', ('正文',), None)
    with bind_log_context(user_id='alice', request_uuid='request-1'):
        handler.handle(record)
    record.msg = 'changed after enqueue'
    writer.flush()
    assert read(tmp_path / 'user/alice/user.log') == '"alice" "request-1" 正文\n'


def test_size_rollover_restart_and_backup_limit(tmp_path):
    config = FileLogConfig(tmp_path, max_bytes=8, backup_count=2)
    for batch in range(2):
        writer = FileLogWriter(config)
        for index in range(3):
            writer.submit('u', f'{batch}-{index}')
        writer.close()
    files = list((tmp_path / 'all').glob('all.*.log'))
    assert len(files) == 2
    assert len(set(p.name for p in files)) == 2
    all_lines = ''.join(read(p) for p in files) + read(tmp_path / 'all/all.log')
    assert '1-2' in all_lines
    assert read(tmp_path / 'all/all.log') == '1-1\n1-2\n'
    assert sorted(all_lines.splitlines()) == [f'{batch}-{index}' for batch in range(2) for index in range(3)]
    writer = FileLogWriter(config)
    for index in range(8):
        writer.submit('u', f'new-{index}')
    writer.close()
    assert len(list((tmp_path / 'all').glob('all.*.log'))) == 2


def test_day_rollover_and_whole_oversized_record(tmp_path):
    now = [time.time()]
    writer = FileLogWriter(FileLogConfig(tmp_path, max_bytes=5), clock=lambda: now[0])
    writer.submit(None, 'single oversized record')
    writer.flush()
    assert read(tmp_path / 'all/all.log') == 'single oversized record\n'
    now[0] += 86400
    writer.submit(None, 'next day')
    writer.close()
    assert len(list((tmp_path / 'all').glob('all.*.log'))) == 1
    assert read(tmp_path / 'all/all.log') == 'next day\n'


def test_day_alone_rolls_small_file(tmp_path):
    now = [time.time()]
    writer = FileLogWriter(FileLogConfig(tmp_path), clock=lambda: now[0])
    writer.submit('u', 'yesterday')
    writer.flush()
    now[0] += 86400
    writer.submit('u', 'today')
    writer.close()
    archives = list((tmp_path / 'all').glob('all.*.log'))
    assert len(archives) == 1 and read(archives[0]) == 'yesterday\n'
    assert read(tmp_path / 'all/all.log') == 'today\n'


def test_existing_unmanaged_file_is_not_adopted(tmp_path, capsys):
    (tmp_path / 'all').mkdir()
    (tmp_path / 'all/all.log').write_text('existing operator file', encoding='utf-8')
    writer = FileLogWriter(FileLogConfig(tmp_path))
    writer.submit(None, 'system event')
    writer.close()
    assert read(tmp_path / 'all/all.log') == 'existing operator file'
    assert not (tmp_path / 'all/all.log.owner').exists()
    assert read(tmp_path / 'system/system.log') == 'system event\n'
    assert '日志文件写入/清理失败' in capsys.readouterr().err


def test_retention_cleans_inactive_users_but_not_unmanaged_files(tmp_path):
    now = [time.time()]
    writer = FileLogWriter(FileLogConfig(tmp_path, retention_days=1), clock=lambda: now[0])
    writer.submit('inactive', 'old')
    writer.flush()
    unrelated = tmp_path / 'user/unmanaged/user.log'
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text('keep', encoding='utf-8')
    now[0] += 2 * 86400
    writer.cleanup()
    writer.close()
    assert not (tmp_path / 'user/inactive/user.log').exists()
    assert not (tmp_path / 'user/inactive/user.log.owner').exists()
    assert read(unrelated) == 'keep'


def test_startup_cleanup(tmp_path):
    writer = FileLogWriter(FileLogConfig(tmp_path))
    writer.submit('old', 'old data')
    writer.close()
    old = time.time() - 30 * 86400
    for path in tmp_path.rglob('*.log'):
        os.utime(path, (old, old))
    writer = FileLogWriter(FileLogConfig(tmp_path))
    writer.close()
    assert not list(tmp_path.rglob('*.log'))


def test_lru_and_threaded_writes(writer, tmp_path):
    def write(index):
        writer.submit(f'user-{index % 5}', f'event-{index}')
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(300)))
    writer.flush()
    assert len([p for p in writer._files if p.parent.parent == tmp_path / 'user']) <= 2
    assert len(writer._user_paths) <= 2
    assert len(read(tmp_path / 'all/all.log').splitlines()) == 300
    for index in range(5):
        assert len(read(tmp_path / f'user/user-{index}/user.log').splitlines()) == 60


def test_queue_full_falls_back_without_loss(writer, tmp_path):
    writer.submit('u', 'queued')
    with patch.object(writer._queue, 'put_nowait', side_effect=queue.Full):
        writer.submit('u', 'fallback')
    writer.close()
    assert sorted(read(tmp_path / 'user/u/user.log').splitlines()) == ['fallback', 'queued']


def test_one_target_failure_does_not_stop_other_or_recurse(writer, tmp_path, capsys):
    append = writer._append
    def fail_all(path, text):
        if path.name == 'all.log':
            raise OSError('disk unavailable')
        append(path, text)
    with patch.object(writer, '_append', side_effect=fail_all):
        writer.submit('u', 'still saved')
        writer.submit('u', 'second record')
        writer.flush()
    assert read(tmp_path / 'user/u/user.log') == 'still saved\nsecond record\n'
    assert capsys.readouterr().err.count('日志文件写入/清理失败') == 1


def test_safe_names_collision_and_restart(writer, tmp_path):
    identities = ['alice', 'ALICE', '../escape', 'CON', '中文用户', 'x' * 300, 'alice.2026-01-01.000001']
    for identity in identities:
        writer.submit(identity, identity)
    writer.close()
    files = list((tmp_path / 'user').glob('*/user.log'))
    assert len(files) == len(identities)
    assert read(tmp_path / 'user/alice/user.log') == 'alice\n'
    writer = FileLogWriter(FileLogConfig(tmp_path))
    for identity in reversed(identities):
        writer.submit(identity, identity)
    writer.close()
    assert {read(p) for p in files} == {f'{identity}\n{identity}\n' for identity in identities}
    assert not (tmp_path.parent / 'escape.log').exists()


def test_disabled_and_idempotent_logger():
    from utils.log import core
    # The offline suite disables files before importing any project logger.
    assert core._writer is None
    first = core.getLogger('test.user-files.repeated')
    second = core.getLogger('test.user-files.repeated')
    assert first is second
    assert len(first.handlers) == 1
    assert not any(thread.name == 'log-files' for thread in threading.enumerate())
