import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mcp_offline_core.execution import Executor


FIXTURES = Path(__file__).resolve().parent / "fixtures"


class HeldCleanupExecutor(Executor):
    def __init__(self, allow_reap):
        super().__init__(timeout_seconds=0.8, max_concurrent_calls=1)
        self.allow_reap = allow_reap
        self.reaped = None

    async def _reap(self, process):
        await self.allow_reap.wait()
        await super()._reap(process)
        self.reaped = process.returncode


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {
            "PYTHONPATH": str(FIXTURES),
            "MCP_TEST_DIRECTORY": str(self.directory),
        }))

    async def test_timeout_returns_while_cleanup_and_handler_are_held(self):
        allow_reap = asyncio.Event()
        executor = HeldCleanupExecutor(allow_reap)
        try:
            async with asyncio.timeout(2):
                result = await executor.call("tool_handlers:slow", {}, frozenset())
            self.assertEqual(result, "tool_timeout")
            self.assertTrue((self.directory / "slow.started").exists())
            self.assertFalse((self.directory / "slow.completed").exists())
            self.assertEqual(await executor.call("tool_handlers:echo", {}, frozenset()), "busy")
            self.assertFalse((self.directory / "echo.called").exists())
        finally:
            allow_reap.set()
            await executor.aclose()
        self.assertIsNotNone(executor.reaped)
        try:
            result = await executor.call("tool_handlers:echo", {"text": "recovered"}, frozenset())
            self.assertEqual(result["echo"], "recovered")
        finally:
            await executor.aclose()

    async def test_rate_limited_before_process_is_started(self):
        executor = Executor(timeout_seconds=3, max_concurrent_calls=6)
        # A released gate lets the four admitted calls finish promptly.
        (self.directory / "slow.release").touch()
        try:
            with patch("asyncio.create_subprocess_exec", wraps=asyncio.create_subprocess_exec) as spawn:
                results = await asyncio.gather(*(
                    executor.call("tool_handlers:slow", {}, frozenset()) for _ in range(5)
                ))
                self.assertEqual(spawn.await_count, 4)
            self.assertEqual(results.count("rate_limited"), 1)
            self.assertEqual(results.count({"late": True}), 4)
        finally:
            await executor.aclose()
