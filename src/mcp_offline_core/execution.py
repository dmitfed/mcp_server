"""Ограничение вызовов, дедлайны и управление дочерними процессами."""

import asyncio
from contextlib import suppress
import json
import sys

from .worker import MAX_MESSAGE_BYTES, encode_message


class Executor:
    """Управлять вызовами инструментов в одном цикле событий MCP-сервера."""

    def __init__(self, timeout_seconds: float, max_concurrent_calls: int):
        """Задать дедлайн вызова, число слотов и начальный запас из четырёх допусков."""
        self._timeout = timeout_seconds
        self._slots = max_concurrent_calls
        self._active = 0
        self._tokens = 4.0
        self._last_refill: float | None = None
        self._cleanup: set[asyncio.Task] = set()

    async def call(self, handler: str, arguments: dict, grants: frozenset[str]) -> dict | str:
        """Вернуть JSON-объект обработчика или фиксированный код отказа.

        Сначала проверяется размер сообщения. Затем диспетчер проверяет свободный
        слот и лимит частоты. Дедлайн отсчитывается от допуска и включает запуск
        дочернего процесса, получение и проверку ответа. Освобождение слота
        выполняется отдельной задачей после сбора процесса.
        """
        try:
            request = encode_message({"handler": handler, "arguments": arguments, "grants": sorted(grants)})
        except (TypeError, ValueError, RecursionError):
            return "invalid_arguments"
        if self._active >= self._slots:
            return "busy"
        loop = asyncio.get_running_loop()
        now = loop.time()
        # Запас восстанавливается со скоростью 10 допусков в секунду и ограничен четырьмя.
        if self._last_refill is not None:
            self._tokens = min(4.0, self._tokens + (now - self._last_refill) * 10)
        self._last_refill = now
        if self._tokens < 1:
            return "rate_limited"
        self._tokens -= 1
        self._active += 1
        deadline = now + self._timeout

        started = asyncio.create_task(
            asyncio.create_subprocess_exec(
                sys.executable, "-m", "mcp_offline_core.worker",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=MAX_MESSAGE_BYTES + 1,
            )
        )
        work = asyncio.create_task(self._exchange(started, request))
        try:
            # Возвращаем таймаут сразу, оставляя завершение исполнителя задаче уборки.
            done, _ = await asyncio.wait({work}, timeout=max(0, deadline - loop.time()))
            if not done or loop.time() > deadline:
                return "tool_timeout"
            return work.result()
        finally:
            cleanup = asyncio.create_task(self._finish(started, work))
            self._cleanup.add(cleanup)
            cleanup.add_done_callback(self._cleanup.discard)

    async def _exchange(self, started: asyncio.Task, request: bytes) -> dict | str:
        """Передать запрос по stdin и проверить одну ограниченную по размеру строку ответа.

        Закрытие stdin отмечает конец запроса. Отмена обмена сохраняет задачу
        запуска, чтобы уборка могла получить ссылку на процесс и завершить его.
        """
        try:
            process = await asyncio.shield(started)
            process.stdin.write(request)
            await process.stdin.drain()
            process.stdin.close()
            line = await process.stdout.readline()
            if not line.endswith(b"\n") or len(line) > MAX_MESSAGE_BYTES + 1:
                return "handler_error"
            response = json.loads(line)
            if not isinstance(response, dict):
                return "handler_error"
            result = response.get("result")
            if isinstance(result, dict):
                encode_message(result)
                return result
            if response.get("error") == "permission_denied":
                return "permission_denied"
        except (OSError, ValueError, RecursionError):
            pass
        return "handler_error"

    async def _finish(self, started: asyncio.Task, work: asyncio.Task) -> None:
        """Остановить обмен, дождаться запуска и освободить слот после сбора процесса.

        Если запуск завершился системной ошибкой, освобождается только слот.
        """
        try:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            try:
                process = await started
            except OSError:
                return
            await self._reap(process)
        finally:
            self._active -= 1

    async def _reap(self, process: asyncio.subprocess.Process) -> None:
        """Закрыть канал запроса, послать SIGTERM и при необходимости SIGKILL.

        На штатное завершение после SIGTERM даётся полсекунды. В обоих
        случаях ожидание выхода завершается до освобождения слота.
        """
        process.stdin.close()
        if process.returncode is not None:
            return
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            async with asyncio.timeout(0.5):
                await process.wait()
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()

    async def aclose(self) -> None:
        """Дождаться всех оставшихся задач уборки при остановке HTTP-сервера."""
        if self._cleanup:
            await asyncio.gather(*self._cleanup)
