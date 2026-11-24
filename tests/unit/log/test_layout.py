"""Directory boundaries and rotation of independently owned log series."""

import hashlib
import os
import time
from datetime import datetime

import pytest

from utils.log.files import FileLogConfig
from utils.log.files import FileLogWriter


def test_size_and_day_rollover_share_sequence_within_each_day(tmp_path):
    now = [time.time()]
    first_day = datetime.fromtimestamp(now[0]).date().isoformat()
    writer = FileLogWriter(FileLogConfig(tmp_path, max_bytes=4), clock=lambda: now[0])
    try:
        for text in ('a1', 'a2'):
            writer.submit('alice', text)
            writer.flush()
        now[0] += 86400
        second_day = datetime.fromtimestamp(now[0]).date().isoformat()
        for text in ('b1', 'b2'):
            writer.submit('alice', text)
            writer.flush()
    finally:
        writer.close()
    for directory, name in ((tmp_path / 'all', 'all'), (tmp_path / 'user/alice', 'user')):
        assert {p.name: p.read_text(encoding='utf-8') for p in directory.glob('*.log')} == {
            f'{name}.{first_day}.000001.log': 'a1\n',
            f'{name}.{first_day}.000002.log': 'a2\n',
            f'{name}.{second_day}.000001.log': 'b1\n',
            f'{name}.log': 'b2\n',
        }


def test_legacy_files_and_markers_are_never_adopted_or_cleaned(tmp_path):
    legacy = ('all.log', 'all.2020-01-01.000001.log', 'system.log',
              'users/alice.log', 'users/alice.2020-01-01.000001.log')
    before = {}
    for name in legacy:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('old data', encoding='utf-8')
        os.utime(path, (1, 1))
        marker = FileLogWriter._owner(path)
        marker.write_text(FileLogWriter._ownership('all'), encoding='utf-8')
        before[path] = path.read_bytes()
        before[marker] = marker.read_bytes()
    writer = FileLogWriter(FileLogConfig(tmp_path, max_bytes=4, backup_count=1))
    for text in ('new-1', 'new-2', 'new-3'):
        writer.submit('alice', text)
        writer.submit(None, text)
    writer.close()
    assert all(path.read_bytes() == data for path, data in before.items())
    assert (tmp_path / 'all/all.log').is_file()
    assert (tmp_path / 'system/system.log').is_file()
    assert (tmp_path / 'user/alice/user.log').is_file()


def test_count_and_age_cleanup_are_scoped_to_each_series(tmp_path):
    now = [time.time()]
    writer = FileLogWriter(FileLogConfig(tmp_path, max_bytes=4, backup_count=2), clock=lambda: now[0])
    try:
        for user in ('alice', 'bob', None):
            for text in ('01', '02', '03', '04', '05'):
                writer.submit(user, text)
        writer.flush()
        directories = ['all', 'system', 'user/alice', 'user/bob']
        for directory in directories:
            assert len(list((tmp_path / directory).glob('*.log'))) == 3
        for path in (tmp_path / 'user/alice').glob('*.log'):
            os.utime(path, (1, 1))
        now[0] += 3601
        writer.cleanup()
        assert list((tmp_path / 'user/alice').iterdir()) == []
        for directory in ('all', 'system', 'user/bob'):
            assert len(list((tmp_path / directory).glob('*.log'))) == 3
        # Empty user directories remain usable when the user becomes active again.
        writer.submit('alice', 'back')
        writer.flush()
        assert (tmp_path / 'user/alice/user.log').read_text(encoding='utf-8') == 'back\n'
    finally:
        writer.close()


def test_unowned_archives_are_not_claimed_or_deleted(tmp_path):
    directory = tmp_path / 'user/alice'
    directory.mkdir(parents=True)
    archive = directory / 'user.2020-01-01.000001.log'
    archive.write_text('unmanaged archive', encoding='utf-8')
    os.utime(archive, (1, 1))
    writer = FileLogWriter(FileLogConfig(tmp_path))
    writer.submit('alice', 'new data')
    writer.close()
    assert archive.read_text(encoding='utf-8') == 'unmanaged archive'
    assert not (directory / 'user.log.owner').exists()
    assert 'new data' in (tmp_path / 'all/all.log').read_text(encoding='utf-8')


@pytest.mark.parametrize(('relative', 'file', 'identity'), [
    ('all', 'all.log', 'all'),
    ('system', 'system.log', 'system'),
    ('user', 'victim/user.log', None),
    ('user/victim', 'user.log', None),
    ('.', 'all/all.log', 'all'),
])
def test_linked_directories_are_not_written_or_cleaned(tmp_path, relative, file, identity):
    root = tmp_path / 'logs'
    outside = tmp_path / 'outside'
    path = outside / file
    path.parent.mkdir(parents=True)
    path.write_text('outside data', encoding='utf-8')
    os.utime(path, (1, 1))
    marker = FileLogWriter._owner(path)
    marker.write_text(FileLogWriter._ownership(
        identity or hashlib.sha256(b'victim').hexdigest(),
    ), encoding='utf-8')
    before = {p.relative_to(outside): p.read_bytes() for p in outside.rglob('*') if p.is_file()}
    link = root / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(outside), str(link))
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        writer = FileLogWriter(FileLogConfig(root))
        writer.submit(None, 'system data')
        writer.submit('victim', 'user data')
        writer.close()
        after = {p.relative_to(outside): p.read_bytes() for p in outside.rglob('*') if p.is_file()}
        assert after == before
    finally:
        # Remove only the link itself, never recursively delete its target.
        if os.name == 'nt':
            link.rmdir()
        else:
            link.unlink()
