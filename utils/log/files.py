"""Single-process, thread-safe file routing and bounded asynchronous writing."""
from __future__ import annotations

import hashlib
import json
import logging
import queue
import re
import stat
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class FileLogConfig:
    directory: Path
    max_bytes: int = 50 * 1024 * 1024
    retention_days: int = 14
    backup_count: int = 20
    user_max_open_files: int = 128
    queue_capacity: int = 1000


class FileLogWriter:
    """One lock protects file state, including synchronous fallback writes."""

    def __init__(self, config: FileLogConfig, *, clock=time.time):
        self.config = config
        self.clock = clock
        self._lock = threading.RLock()
        self._accept_lock = threading.Lock()
        self._files = OrderedDict()
        self._user_paths = OrderedDict()
        self._queue = queue.Queue(maxsize=config.queue_capacity)
        self._closed = False
        self._last_warning = float('-inf')
        self._next_cleanup = 0.0
        self._thread = threading.Thread(target=self._run, name='log-files', daemon=True)
        self._thread.start()

    def _warn(self, error):
        now = time.monotonic()
        if now - self._last_warning >= 60:
            self._last_warning = now
            try:
                sys.stderr.write(f'日志文件写入/清理失败: error_type={type(error).__name__}\n')
            except Exception:
                pass

    def submit(self, user_id: str | None, text: str):
        with self._accept_lock:
            if self._closed:
                return
            try:
                self._queue.put_nowait((user_id, text))
            except queue.Full:
                self._write(user_id, text)

    def _run(self):
        self.cleanup()
        while True:
            try:
                item = self._queue.get(timeout=60)
            except queue.Empty:
                self.cleanup()
                continue
            try:
                if item is None:
                    return
                self._write(*item)
            finally:
                self._queue.task_done()

    def flush(self):
        self._queue.join()

    def close(self):
        with self._accept_lock:
            if self._closed:
                return
            self._closed = True
            self._queue.put(None)
        self._thread.join()
        with self._lock:
            for path in list(self._files):
                self._close_file(path)

    def _close_file(self, path):
        state = self._files.pop(path, None)
        if state:
            try:
                state[0].close()
            except Exception as error:
                self._warn(error)

    @staticmethod
    def _owner(path):
        return path.with_name(path.name + '.owner')

    @staticmethod
    def _ownership(identity):
        return json.dumps({'shulan_log': 1, 'identity': identity}, sort_keys=True)

    @staticmethod
    def _is_link(path):
        try:
            info = path.lstat()
        except FileNotFoundError:
            return False
        return stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT,
        )

    def _check_path(self, path):
        # Check ancestors too: on Windows a directory junction is not a symlink.
        for component in reversed((path.absolute(), *path.absolute().parents)):
            if self._is_link(component):
                raise ValueError('linked log paths are not supported')

    def _ensure_directory(self, directory):
        self._check_path(directory)
        directory.mkdir(parents=True, exist_ok=True)

    def _claim(self, path, identity):
        marker = self._owner(path)
        self._check_path(path)
        self._check_path(marker)
        expected = self._ownership(identity)
        if marker.exists():
            if marker.read_text(encoding='utf-8') != expected:
                raise ValueError('log file ownership mismatch')
        else:
            if path.exists() or self._archives(path):
                raise ValueError('unmanaged log file already exists')
            marker.write_text(expected, encoding='utf-8')

    def _user_path(self, user_id):
        cached = self._user_paths.get(user_id)
        if cached is not None and cached in self._files:
            self._user_paths.move_to_end(user_id)
            return cached
        directory = self.config.directory / 'user'
        self._ensure_directory(directory)
        digest = hashlib.sha256(user_id.encode('utf-8')).hexdigest()
        safe = re.sub(r'[^\w.-]', '_', user_id).strip('. ')
        reserved = safe.split('.')[0].upper() in {
            'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10)),
        }
        archive_name = re.search(r'\.\d{4}-\d{2}-\d{2}\.\d+$', safe)
        if safe != user_id or not safe or reserved or archive_name or len(safe.encode('utf-8')) > 120:
            safe = re.sub(r'[^a-zA-Z0-9_-]', '_', user_id)[:40] + '~' + digest
        path = directory / safe / 'user.log'
        fallback = directory / f'{safe[:40]}~{digest}' / 'user.log'
        fallback_marker = self._owner(fallback)
        self._check_path(fallback_marker)
        if fallback_marker.exists() and fallback_marker.read_text(encoding='utf-8') == self._ownership(digest):
            path = fallback
        marker = self._owner(path)
        self._check_path(marker)
        if not marker.exists():
            conflict = any(p.name != path.parent.name and p.name.casefold() == path.parent.name.casefold()
                           for p in directory.iterdir())
        else:
            conflict = marker.read_text(encoding='utf-8') != self._ownership(digest)
        if conflict:
            path = fallback
        self._ensure_directory(path.parent)
        self._claim(path, digest)
        self._user_paths[user_id] = path
        self._user_paths.move_to_end(user_id)
        while len(self._user_paths) > self.config.user_max_open_files:
            self._user_paths.popitem(last=False)
        return path

    def _write(self, user_id, text):
        with self._lock:
            try:
                self._ensure_directory(self.config.directory)
            except Exception as error:
                self._warn(error)
                return
            for target in ('all', 'user' if user_id else 'system'):
                path = None
                try:
                    if target == 'user':
                        path = self._user_path(user_id)
                    else:
                        path = self.config.directory / target / f'{target}.log'
                        self._ensure_directory(path.parent)
                        if path not in self._files:
                            self._claim(path, target)
                    self._append(path, text)
                except Exception as error:
                    if path is not None:
                        self._close_file(path)
                    self._warn(error)
            self.cleanup()

    def _archives(self, path, candidates=None):
        pattern = re.compile(re.escape(path.stem) + r'\.\d{4}-\d{2}-\d{2}\.\d+\.log$')
        return sorted(
            (p for p in (path.parent.iterdir() if candidates is None else candidates)
             if pattern.fullmatch(p.name) and not self._is_link(p) and p.is_file()
             and not self._owner(p).exists()),
            key=lambda p: (p.stat().st_mtime, p.name),
        )

    def _append(self, path, text):
        self._check_path(path)
        now = self.clock()
        today = datetime.fromtimestamp(now).date().isoformat()
        state = self._files.get(path)
        if state is None:
            old_date = datetime.fromtimestamp(path.stat().st_mtime).date().isoformat() if path.exists() else today
            stream = path.open('ab')
            state = [stream, old_date, path.stat().st_size]
            self._files[path] = state
        self._files.move_to_end(path)
        data = (text + '\n').encode('utf-8', errors='backslashreplace')
        if state[2] and (state[1] != today or state[2] + len(data) > self.config.max_bytes):
            date = state[1]
            self._close_file(path)
            prefix = f'{path.stem}.{date}.'
            index = 1 + max((int(p.stem.rsplit('.', 1)[1]) for p in self._archives(path)
                             if p.name.startswith(prefix)), default=0)
            archive = path.with_name(f'{path.stem}.{date}.{index:06d}.log')
            while archive.exists() or self._is_link(archive):
                index += 1
                archive = path.with_name(f'{path.stem}.{date}.{index:06d}.log')
            path.rename(archive)
            state = [path.open('ab'), today, 0]
            self._files[path] = state
            self._clean_series(path, now)
        state[0].write(data)
        state[0].flush()
        state[2] += len(data)
        users = [p for p in self._files if p.parent.parent == self.config.directory / 'user']
        for old in users[:-self.config.user_max_open_files]:
            self._close_file(old)

    def _clean_series(self, path, now, candidates=None):
        cutoff = now - self.config.retention_days * 86400
        archives = self._archives(path, candidates)
        excess = max(0, len(archives) - self.config.backup_count)
        for index, archive in enumerate(archives):
            if index < excess or archive.stat().st_mtime < cutoff:
                archive.unlink()
        if path.exists() and not self._is_link(path) and path.stat().st_mtime < cutoff:
            self._close_file(path)
            path.unlink()
        return any(archive.exists() for archive in archives)

    def cleanup(self):
        with self._lock:
            now = self.clock()
            if now < self._next_cleanup:
                return
            self._next_cleanup = now + 3600
            paths = [self.config.directory / name / f'{name}.log' for name in ('all', 'system')]
            user_root = self.config.directory / 'user'
            try:
                self._check_path(user_root)
                if user_root.is_dir():
                    paths.extend(child / 'user.log' for child in user_root.iterdir()
                                 if not self._is_link(child) and child.is_dir())
            except Exception as error:
                self._warn(error)
            for path in paths:
                try:
                    marker = self._owner(path)
                    self._check_path(marker)
                    self._check_path(path)
                    if not marker.is_file():
                        continue
                    owner = json.loads(marker.read_text(encoding='utf-8'))
                    identity = owner.get('identity')
                    expected = path.stem if path.stem in ('all', 'system') else None
                    if owner.get('shulan_log') != 1:
                        continue
                    if expected is not None:
                        valid_identity = identity == expected
                    else:
                        valid_identity = isinstance(identity, str) and re.fullmatch(r'[0-9a-f]{64}', identity)
                    if not valid_identity:
                        continue
                    remaining = self._clean_series(path, now)
                    if not path.exists() and not remaining:
                        marker.unlink()
                except Exception as error:
                    self._warn(error)


class FileLogHandler(logging.Handler):
    def __init__(self, writer):
        super().__init__()
        self.writer = writer

    def emit(self, record):
        try:
            self.writer.submit(getattr(record, 'log_user_id', None), self.format(record))
        except Exception as error:
            self.writer._warn(error)
