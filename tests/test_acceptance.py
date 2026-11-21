import asyncio
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx2

from mcp_offline_core import acceptance


class AcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_retries_refusal_then_rejects_redirect_and_reaps_child(self):
        real_http_client = httpx2.AsyncClient
        attempts = []

        async def respond(request):
            attempts.append(request.url.path)
            if len(attempts) == 1:
                raise httpx2.ConnectError("connection refused", request=request)
            return httpx2.Response(307, headers={"Location": "/redirect"})

        def make_http(**kwargs):
            return real_http_client(transport=httpx2.MockTransport(respond), **kwargs)

        process = Mock(returncode=None, wait=AsyncMock())
        process.stdout = asyncio.StreamReader()
        process.stdout.feed_data(b'{"mcp_url":"http://127.0.0.1:8765/mcp"}\n')
        process.stdout.feed_eof()
        with (
            patch("asyncio.create_subprocess_exec", return_value=process),
            patch.object(acceptance.httpx2, "AsyncClient", side_effect=make_http),
        ):
            with self.assertRaisesRegex(acceptance.CheckError, "discovery failed"):
                async with acceptance._mcp(Path("demo.toml")):
                    self.fail("redirected discovery accepted")
        self.assertEqual(attempts, ["/mcp", "/mcp"])
        process.terminate.assert_called_once()
        process.wait.assert_awaited_once()
