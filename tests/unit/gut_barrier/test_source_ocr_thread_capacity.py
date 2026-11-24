"""Offline thread-lifetime checks: async timeout cannot stop a blocking SDK."""
import asyncio
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.gut_barrier import sources as source_module
from adapters.gut_barrier.sources import GutBarrierSources
from modules.gut_barrier.schemas import GutBarrierRequest


class BlockingParser:
    def __init__(self, *, stage, capacity):
        self.stage, self.capacity = stage, capacity
        self.lock = threading.Lock()
        self.started, self.release = threading.Event(), threading.Event()
        self.active = self.peak = 0
        self.submitted = []

    def _block(self):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            if self.active == self.capacity:
                self.started.set()
        try:
            assert self.release.wait(3), "offline test must release its SDK threads"
        finally:
            with self.lock:
                self.active -= 1

    def submit_local_file(self, file_path, *, file_name=None, file_type=None):
        assert Path(file_path).is_file()
        with self.lock:
            self.submitted.append(file_path)
            task_id = str(len(self.submitted))
        if self.stage == "submit":
            self._block()
        return task_id

    def get_result(self, task_id, *, poll_interval=1):
        if self.stage == "get_result":
            self._block()
        return {"success": True, "text": "姓名：李明\n采样日期：2026-09-24\nDAO 17 U/L"}


def source_request(index):
    return GutBarrierRequest(userId="offline", sessionId=str(index), name="李明", fileList=[{
        "url": f"https://offline.invalid/scan-{index}.jpg", "fileType": "image",
    }])


async def returned_slots(source, capacity):
    """Wait for actual worker-finally release, not merely parser method return."""
    for _ in range(100):
        acquired = 0
        try:
            while acquired < capacity and source._ocr_slots.acquire(blocking=False):
                acquired += 1
            if acquired == capacity:
                return
        finally:
            for _ in range(acquired):
                source._ocr_slots.release()
        await asyncio.sleep(0.01)
    pytest.fail("synchronous OCR slots did not return")


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["submit", "get_result"])
@pytest.mark.parametrize("capacity", [1, 2])
async def test_timed_out_sdk_calls_keep_real_slots_across_reports_and_recover(monkeypatch, stage, capacity):
    monkeypatch.setattr(source_module, "OCR_PAGE_TIMEOUT_SECONDS", 0.1)
    parser = BlockingParser(stage=stage, capacity=capacity)
    source = GutBarrierSources(concurrency=capacity, parsing_client=parser,
        downloader=SimpleNamespace(download=AsyncMock(return_value=b"\xff\xd8\xffsynthetic-image")))
    first = [asyncio.create_task(source.collect(source_request(index), evaluation_date="2026-09-24"))
             for index in range(capacity)]
    try:
        assert await asyncio.to_thread(parser.started.wait, 1)
        timed_out = await asyncio.wait_for(asyncio.gather(*first), 1)
        assert all(any(issue.get("errorType") == "TimeoutError" for issue in row["issues"])
                   for row in timed_out)
        assert parser.active == parser.peak == capacity

        # The async source slots have returned, but the old SDK calls have not.
        # New reports must fail this page promptly without more submissions.
        following = await asyncio.wait_for(asyncio.gather(*(
            source.collect(source_request(index + 10), evaluation_date="2026-09-24")
            for index in range(6))), 1)
        assert all(any(issue["code"] == "OCR_CAPACITY_EXHAUSTED" for issue in row["issues"])
                   for row in following)
        assert all(row["sources"][0]["pageAudit"][0]["ocrStatus"] == "unavailable" for row in following)
        assert len(parser.submitted) == capacity
        assert parser.active == parser.peak == capacity
        if stage == "get_result":
            assert all(not Path(path).exists() for path in parser.submitted)
    finally:
        parser.release.set()
        await asyncio.gather(*first, return_exceptions=True)
        await returned_slots(source, capacity)
    assert all(not Path(path).exists() for path in parser.submitted)

    recovered = await source.collect(source_request(99), evaluation_date="2026-09-24")
    assert recovered["issues"] == []
    assert [(row["code"], row["value"]) for row in recovered["observations"]] == [("DAO", "17")]
    assert len(parser.submitted) == capacity + 1
    assert parser.peak == capacity
    assert all(not Path(path).exists() for path in parser.submitted)


@pytest.mark.asyncio
async def test_async_cancellation_propagates_without_releasing_the_running_sdk_slot():
    parser = BlockingParser(stage="submit", capacity=1)
    source = GutBarrierSources(concurrency=1, parsing_client=parser,
        downloader=SimpleNamespace(download=AsyncMock(return_value=b"\xff\xd8\xffsynthetic-image")))
    task = asyncio.create_task(source.collect(source_request(1), evaluation_date="2026-09-24"))
    try:
        assert await asyncio.to_thread(parser.started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert parser.active == 1
        blocked = await source.collect(source_request(2), evaluation_date="2026-09-24")
        assert any(issue["code"] == "OCR_CAPACITY_EXHAUSTED" for issue in blocked["issues"])
        assert len(parser.submitted) == 1
    finally:
        parser.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await returned_slots(source, 1)
    assert all(not Path(path).exists() for path in parser.submitted)
