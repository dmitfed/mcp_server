import asyncio
from contextlib import contextmanager
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import selectors
import socket
import subprocess
import sysconfig
import tempfile
import time
import unittest
from urllib.parse import urlsplit

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError


def server_command(config, port=0):
    return [str(Path(sysconfig.get_path("scripts")) / "mcp-core"), "--config", str(config), "--port", str(port)]


@contextmanager
def running_server(config, env=None):
    with tempfile.TemporaryFile() as diagnostics, subprocess.Popen(
        server_command(config), stdout=subprocess.PIPE, stderr=diagnostics, env=env,
    ) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                if not selector.select(10):
                    raise AssertionError("server did not announce its address")
                line = process.stdout.readline()
            if not line:
                diagnostics.seek(0)
                raise AssertionError(f"server exited: {diagnostics.read().decode()}")
            url = json.loads(line)["mcp_url"]
            address = urlsplit(url)
            if address.scheme != "http" or address.hostname != "127.0.0.1" or address.path != "/mcp":
                raise AssertionError(f"unexpected server address: {url}")
            deadline = time.monotonic() + 3
            while True:
                try:
                    with socket.create_connection((address.hostname, address.port), timeout=0.2):
                        break
                except OSError:
                    if time.monotonic() >= deadline or process.poll() is not None:
                        raise
                    time.sleep(0.02)
            yield url, diagnostics
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def post_rpc(url, body, headers=(), *, skip_host=False):
    address = urlsplit(url)
    connection = HTTPConnection(address.hostname, address.port, timeout=5)
    try:
        payload = json.dumps(body).encode()
        connection.putrequest("POST", address.path, skip_host=skip_host)
        connection.putheader("Accept", "application/json, text/event-stream")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(len(payload)))
        for name, value in headers:
            connection.putheader(name, value)
        connection.endheaders(payload)
        reply = connection.getresponse()
        return reply.status, reply.read()
    finally:
        connection.close()


class EmptyServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.directory / "empty.toml"
        self.config.write_text("config_version = 1\nmodules = []\n")

    def test_discovery_and_empty_list(self):
        async def check(url):
            async with httpx2.AsyncClient(trust_env=False) as http:
                transport = streamable_http_client(url, http_client=http)
                async with asyncio.timeout(10), Client(transport, cache=None) as client:
                    self.assertEqual(client.protocol_version, "2026-07-28")
                    self.assertIsNotNone(client.server_capabilities.tools)
                    self.assertEqual((await client.list_tools()).tools, [])

        with running_server(self.config) as (url, _):
            asyncio.run(check(url))

    def test_bad_host_and_any_origin_are_rejected(self):
        body = {"jsonrpc": "2.0", "id": 1, "method": "server/discover"}
        with running_server(self.config) as (url, _):
            status, _ = post_rpc(url, body, [("Host", "outside.example")], skip_host=True)
            self.assertEqual(status, 421)
            status, _ = post_rpc(url, body, skip_host=True)
            self.assertIn(status, (400, 421))
            for origin in ("http://outside.example", ""):
                with self.subTest(origin=origin):
                    status, _ = post_rpc(url, body, [("Origin", origin)])
                    self.assertEqual(status, 403)

    def test_missing_configuration(self):
        missing = self.directory / "missing.toml"
        result = subprocess.run(server_command(missing), capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"missing.toml", result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)

    def test_occupied_port(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            result = subprocess.run(
                server_command(self.config, occupied.getsockname()[1]), capture_output=True, timeout=10,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"cannot bind", result.stderr)


class ModuleServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.fixtures = Path(__file__).resolve().parent / "fixtures"
        self.env = {
            **os.environ,
            "PYTHONPATH": str(self.fixtures),
            "MCP_TEST_DIRECTORY": str(self.directory),
        }
        self.config = self.directory / "server.toml"
        self.write_config()

    def write_config(self, disabled=(), second_manifest=None):
        config = (
            "config_version = 1\n"
            f"disabled_tools = {json.dumps(disabled)}\n"
            "[execution]\ntimeout_seconds = 0.8\n"
            f"[[modules]]\nmanifest = {json.dumps(str(self.fixtures / 'module-v1.json'))}\n"
        )
        if second_manifest is not None:
            config += f"[[modules]]\nmanifest = {json.dumps(str(second_manifest))}\n"
        self.config.write_text(config)

    def test_negative_calls_fail_without_protected_side_effects(self):
        self.write_config(disabled=("fixture_echo",))

        async def check(url):
            async with httpx2.AsyncClient(trust_env=False) as http:
                async with asyncio.timeout(10), Client(streamable_http_client(url, http_client=http), cache=None) as client:
                    listed = {tool.name for tool in (await client.list_tools()).tools}
                    self.assertNotIn("fixture_echo", listed)
                    for name in ("fixture_echo", "unknown_tool"):
                        with self.subTest(name=name), self.assertRaises(MCPError) as error:
                            await client.call_tool(name, {"text": "PRIVATE-ARGUMENT"})
                        self.assertEqual(error.exception.code, -32602)
                    result = await client.call_tool("fixture_write", {})
                    self.assertTrue(result.model_dump(by_alias=True)["isError"])

        with running_server(self.config, self.env) as (url, _):
            asyncio.run(check(url))
            body = {
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {
                    "name": "fixture_write", "arguments": [],
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {},
                    },
                },
            }
            _, response = post_rpc(url, body, [
                ("Mcp-Protocol-Version", "2026-07-28"), ("Mcp-Method", "tools/call"), ("Mcp-Name", "fixture_write"),
            ])
            self.assertEqual(json.loads(response)["error"]["code"], -32602)
            self.assertFalse((self.directory / "echo.called").exists())
            self.assertFalse((self.directory / "protected.called").exists())

    def test_error_timeout_recovery_and_private_logs(self):
        async def check(url):
            async with httpx2.AsyncClient(trust_env=False) as http:
                async with asyncio.timeout(12), Client(streamable_http_client(url, http_client=http), cache=None) as client:
                    bad = await client.call_tool("fixture_echo", {"text": 42})
                    self.assertTrue(bad.model_dump(by_alias=True)["isError"])
                    self.assertFalse((self.directory / "echo.called").exists())
                    for name in ("fixture_fail", "fixture_slow"):
                        result = await client.call_tool(name, {})
                        self.assertTrue(result.model_dump(by_alias=True)["isError"])
                        self.assertIn("fixture_echo", {tool.name for tool in (await client.list_tools()).tools})
                        echoed = await client.call_tool("fixture_echo", {"text": "PRIVATE-ARGUMENT"})
                        self.assertEqual(echoed.model_dump(by_alias=True)["structuredContent"]["echo"], "PRIVATE-ARGUMENT")

        with running_server(self.config, self.env) as (url, diagnostics):
            asyncio.run(check(url))
            diagnostics.seek(0)
            log = diagnostics.read().decode()
        records = [json.loads(line) for line in log.splitlines()]
        self.assertIn(("ok", "ok"), {(row["status"], row["code"]) for row in records})
        self.assertTrue({"handler_error", "tool_timeout"}.issubset({row["code"] for row in records}))
        self.assertNotIn("PRIVATE-", log)

    def test_duplicate_name_reports_conflicting_tool(self):
        extra = self.directory / "duplicate.json"
        manifest = json.loads((self.fixtures / "module-v1.json").read_text())
        extra.write_text(json.dumps(dict(manifest, module_id="another")))
        self.write_config(second_manifest=extra)
        process = subprocess.run(server_command(self.config), capture_output=True, timeout=10)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(process.stdout, b"")
        self.assertIn(b"fixture_echo", process.stderr)
