"""Single reusable Dotabuff browser worker.

Two halves in one module:

- Worker process (``python -m src.browser_worker``): owns exactly one Playwright
  driver + persistent Chromium context, serves fetch requests over a JSON-lines
  stdin/stdout protocol. Any internal failure exits the process — cleanup is
  the parent's job, not a best-effort ``finally``.
- :class:`BrowserWorkerSupervisor`: parent-side client. Spawns the worker in its
  own session (process group), serializes requests, enforces a hard deadline
  per request, and on timeout/protocol failure SIGKILLs the whole process
  group — node driver, chromium, zygotes, renderers, everything.

This replaces the old launch-per-fetch flow whose cancellation via
``asyncio.wait_for`` orphaned playwright driver processes.
"""

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from .config import (
    DOTABUFF_BROWSER_ARTIFACTS,
    DOTABUFF_BROWSER_IDLE_SHUTDOWN_SEC,
    DOTABUFF_BROWSER_OUTPUT_DIR,
    DOTABUFF_BROWSER_OUTPUT_KEEP,
    DOTABUFF_BROWSER_PROFILE_DIR,
    DOTABUFF_BROWSER_SETTLE_TIMEOUT_MS,
    DOTABUFF_BROWSER_TIMEOUT_MS,
    DOTABUFF_BROWSER_TOTAL_TIMEOUT_MS,
    DOTABUFF_BROWSER_WAIT_MS,
    DOTABUFF_BROWSER_WS_ENDPOINT,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parent side
# ---------------------------------------------------------------------------


class BrowserWorkerError(Exception):
    """Fetch failed; the worker has been killed and will respawn lazily."""


class BrowserWorkerSupervisor:
    """Owns at most one worker subprocess; hard-kills its process group on any doubt."""

    def __init__(
        self,
        *,
        total_timeout_sec: float = DOTABUFF_BROWSER_TOTAL_TIMEOUT_MS / 1000,
        idle_shutdown_sec: float = DOTABUFF_BROWSER_IDLE_SHUTDOWN_SEC,
        output_dir: str = DOTABUFF_BROWSER_OUTPUT_DIR,
        output_keep: int = DOTABUFF_BROWSER_OUTPUT_KEEP,
        worker_cmd: Optional[list] = None,
    ):
        self._total_timeout_sec = total_timeout_sec
        self._idle_shutdown_sec = idle_shutdown_sec
        self._output_dir = Path(output_dir)
        self._output_keep = output_keep
        self._worker_cmd = worker_cmd or [sys.executable, "-m", "src.browser_worker"]
        self._lock = asyncio.Lock()
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._idle_task: Optional[asyncio.Task] = None
        self._closed = False

    async def fetch_player_matches(self, player_id: str, limit: int) -> Dict[str, Any]:
        """Fetch via the resident worker. Raises BrowserWorkerError on failure."""
        async with self._lock:
            if self._closed:
                raise BrowserWorkerError("supervisor closed")
            await self._ensure_worker()
            request = json.dumps({"cmd": "fetch", "player_id": str(player_id), "limit": int(limit)})
            try:
                self._proc.stdin.write(request.encode() + b"\n")
                await self._proc.stdin.drain()
                line = await asyncio.wait_for(
                    self._proc.stdout.readline(), timeout=self._total_timeout_sec
                )
            except (asyncio.TimeoutError, ConnectionError, BrokenPipeError, ValueError) as e:
                # ValueError covers LimitOverrunError: a response line that
                # exceeds the stream limit desyncs the protocol — kill and respawn.
                await self._kill_worker()
                raise BrowserWorkerError(f"worker timeout/pipe failure: {type(e).__name__}") from e

            if not line:
                await self._kill_worker()
                raise BrowserWorkerError("worker died (EOF)")
            try:
                response = json.loads(line)
            except json.JSONDecodeError as e:
                await self._kill_worker()
                raise BrowserWorkerError("worker produced garbage") from e

            self._touch_idle_timer()
            if not response.get("ok"):
                # Worker survived but the fetch failed inside the page; worker
                # already recycled its own context or exited. Keep it if alive.
                raise BrowserWorkerError(str(response.get("error") or "fetch failed"))
            return response.get("result") or {}

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            if self._idle_task is not None:
                self._idle_task.cancel()
                self._idle_task = None
            await self._kill_worker()

    async def _ensure_worker(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        self._proc = None
        self._prune_artifacts()
        self._proc = await asyncio.create_subprocess_exec(
            *self._worker_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,  # inherit: worker logs land in the bot's stderr
            start_new_session=True,  # own process group => killpg reaps the full tree
            limit=4 * 1024 * 1024,  # response JSON can exceed the 64KB default line limit
        )
        logger.info("Browser worker spawned (pid=%s)", self._proc.pid)

    async def _kill_worker(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None or proc.returncode is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            logger.error("Browser worker pid=%s did not reap after SIGKILL", proc.pid)
        logger.info("Browser worker killed (pid=%s)", proc.pid)

    def _touch_idle_timer(self) -> None:
        if self._idle_shutdown_sec <= 0:
            return
        if self._idle_task is not None:
            self._idle_task.cancel()
        self._idle_task = asyncio.get_running_loop().create_task(self._idle_shutdown())

    async def _idle_shutdown(self) -> None:
        try:
            await asyncio.sleep(self._idle_shutdown_sec)
        except asyncio.CancelledError:
            return
        async with self._lock:
            if self._proc is not None:
                logger.info("Browser worker idle %.0fs; shutting down", self._idle_shutdown_sec)
                await self._kill_worker()

    def _prune_artifacts(self) -> None:
        """Bounded cleanup of debug artifacts (screenshots/html/json)."""
        try:
            files = sorted(
                (p for p in self._output_dir.iterdir() if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for stale in files[self._output_keep:]:
                stale.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Worker side
# ---------------------------------------------------------------------------


async def _worker_main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [browser-worker] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    from .dotabuff_playwright import DotabuffBrowserClient

    client = DotabuffBrowserClient(
        profile_dir=Path(DOTABUFF_BROWSER_PROFILE_DIR),
        timeout_ms=DOTABUFF_BROWSER_TIMEOUT_MS,
        headless=True,
        ws_endpoint=DOTABUFF_BROWSER_WS_ENDPOINT or None,
    )
    await client.__aenter__()
    logger.info("Browser context ready")

    reader = asyncio.StreamReader()
    await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
    )

    while True:
        line = await reader.readline()
        if not line:
            break  # parent closed stdin / died
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"ok": False, "error": "bad request"}), flush=True)
            continue
        if request.get("cmd") == "shutdown":
            break
        if request.get("cmd") != "fetch":
            print(json.dumps({"ok": False, "error": "unknown cmd"}), flush=True)
            continue
        try:
            result = await client.fetch_player_matches(
                str(request["player_id"]),
                limit=int(request.get("limit") or 5),
                output_dir=Path(DOTABUFF_BROWSER_OUTPUT_DIR),
                wait_after_load_ms=DOTABUFF_BROWSER_WAIT_MS,
                settle_timeout_ms=DOTABUFF_BROWSER_SETTLE_TIMEOUT_MS,
                save_artifacts=DOTABUFF_BROWSER_ARTIFACTS,
            )
            print(json.dumps({"ok": True, "result": result}, ensure_ascii=False), flush=True)
        except Exception as e:  # noqa: BLE001 — one bad fetch must not wedge the loop
            logger.warning("Fetch failed: %s", e)
            print(json.dumps({"ok": False, "error": str(e) or type(e).__name__}), flush=True)
            # A dead page usually means a dead browser; exit and let the
            # supervisor respawn a clean worker rather than limp along.
            if "closed" in str(e).lower():
                break

    await client.__aexit__(None, None, None)
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_worker_main()))


if __name__ == "__main__":
    main()
