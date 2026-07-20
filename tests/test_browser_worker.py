import asyncio
import json
import sys

import pytest

from src.browser_worker import BrowserWorkerError, BrowserWorkerSupervisor
from src.config import (
    DOTABUFF_BROWSER_SETTLE_TIMEOUT_MS,
    DOTABUFF_BROWSER_TIMEOUT_MS,
    DOTABUFF_BROWSER_TOTAL_TIMEOUT_MS,
    DOTABUFF_BROWSER_WAIT_MS,
)

ECHO_WORKER = """
import json, sys, os
for line in sys.stdin:
    req = json.loads(line)
    print(json.dumps({"ok": True, "result": {"player_id": req["player_id"], "pid": os.getpid(), "rows": []}}), flush=True)
"""

HANG_WORKER = """
import sys, time
sys.stdin.readline()
time.sleep(600)
"""

DIE_WORKER = """
import sys
sys.stdin.readline()
sys.exit(1)
"""


def _make_supervisor(tmp_path, script: str, **kwargs) -> BrowserWorkerSupervisor:
    worker_path = tmp_path / "stub_worker.py"
    worker_path.write_text(script)
    kwargs.setdefault("output_dir", str(tmp_path / "out"))
    kwargs.setdefault("idle_shutdown_sec", 0)
    return BrowserWorkerSupervisor(
        worker_cmd=[sys.executable, str(worker_path)], **kwargs
    )


@pytest.mark.asyncio
async def test_fetch_roundtrip_and_worker_reuse(tmp_path):
    supervisor = _make_supervisor(tmp_path, ECHO_WORKER)
    try:
        first = await supervisor.fetch_player_matches("111", 5)
        second = await supervisor.fetch_player_matches("222", 5)
        assert first["player_id"] == "111"
        assert second["player_id"] == "222"
        # Same worker process served both requests — that's the whole point.
        assert first["pid"] == second["pid"]
    finally:
        await supervisor.aclose()


@pytest.mark.asyncio
async def test_timeout_kills_worker_and_respawns(tmp_path):
    supervisor = _make_supervisor(tmp_path, HANG_WORKER, total_timeout_sec=0.5)
    try:
        old_proc = None
        with pytest.raises(BrowserWorkerError):
            await supervisor.fetch_player_matches("111", 5)

        # The hung worker must be gone (killed, reaped — no zombie).
        assert supervisor._proc is None

        # Next fetch spawns a fresh worker instead of wedging forever.
        with pytest.raises(BrowserWorkerError):
            await supervisor.fetch_player_matches("222", 5)
    finally:
        await supervisor.aclose()


@pytest.mark.asyncio
async def test_worker_death_surfaces_as_error(tmp_path):
    supervisor = _make_supervisor(tmp_path, DIE_WORKER, total_timeout_sec=5)
    try:
        with pytest.raises(BrowserWorkerError, match="EOF|died"):
            await supervisor.fetch_player_matches("111", 5)
        assert supervisor._proc is None
    finally:
        await supervisor.aclose()


@pytest.mark.asyncio
async def test_closed_supervisor_refuses_fetches(tmp_path):
    supervisor = _make_supervisor(tmp_path, ECHO_WORKER)
    await supervisor.aclose()
    with pytest.raises(BrowserWorkerError, match="closed"):
        await supervisor.fetch_player_matches("111", 5)


@pytest.mark.asyncio
async def test_idle_shutdown_reaps_worker(tmp_path):
    supervisor = _make_supervisor(tmp_path, ECHO_WORKER, idle_shutdown_sec=0.3)
    try:
        result = await supervisor.fetch_player_matches("111", 5)
        assert result["player_id"] == "111"
        assert supervisor._proc is not None
        await asyncio.sleep(0.8)
        assert supervisor._proc is None
    finally:
        await supervisor.aclose()


@pytest.mark.asyncio
async def test_artifact_pruning_on_spawn(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    for i in range(10):
        (out_dir / f"artifact_{i:02d}.png").write_text("x")
    supervisor = _make_supervisor(tmp_path, ECHO_WORKER, output_keep=3)
    try:
        await supervisor.fetch_player_matches("111", 5)
        assert len(list(out_dir.iterdir())) == 3
    finally:
        await supervisor.aclose()


def test_total_timeout_covers_inner_worst_case():
    # Regression guard for the original leak: the outer deadline (15s) was
    # shorter than a single page's inner timeouts (45s), so every fetch was
    # cancelled mid-flight. Two page visits + launch slack must fit.
    inner_worst_case_ms = 2 * (
        DOTABUFF_BROWSER_TIMEOUT_MS
        + DOTABUFF_BROWSER_WAIT_MS
        + DOTABUFF_BROWSER_SETTLE_TIMEOUT_MS
    )
    assert DOTABUFF_BROWSER_TOTAL_TIMEOUT_MS >= inner_worst_case_ms + 10_000
