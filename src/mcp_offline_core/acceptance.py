"""Local acceptance host for the MCP core and the first model profile."""

import argparse
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from importlib.metadata import version
import json
import logging
from pathlib import Path
import subprocess
import sys
import sysconfig
from urllib.parse import urlsplit

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


PROTOCOL = "2026-07-28"
MODEL_URL = "http://127.0.0.1:9931/v1/chat/completions"
DEMO_TOOL = "demo_echo"
DEMO_ARGUMENTS = {"text": "mcp-check-001"}
DEMO_RESULT = {"echo": "mcp-check-001"}


class CheckError(RuntimeError):
    """A fixed description of a failed acceptance step."""


def _read_url(record: bytes) -> str:
    url = json.loads(record)["mcp_url"]
    address = urlsplit(url)
    port = address.port
    if not port or url != f"http://127.0.0.1:{port}/mcp":
        raise CheckError("invalid MCP startup address")
    return url


def _only_connect_errors(error: Exception) -> bool:
    if isinstance(error, ExceptionGroup):
        return error.subgroup(lambda exc: not isinstance(exc, (BaseExceptionGroup, httpx2.ConnectError))) is None
    return isinstance(error, httpx2.ConnectError)


async def _reject_redirect(response: httpx2.Response) -> None:
    # The SDK can follow same-origin redirects independently of httpx2.
    if response.is_redirect:
        response.raise_for_status()


async def _discover(stack: AsyncExitStack, http: httpx2.AsyncClient, url: str, process) -> Client:
    while process.returncode is None:
        try:
            client = await stack.enter_async_context(
                Client(streamable_http_client(url, http_client=http), cache=None)
            )
        except Exception as error:
            if not _only_connect_errors(error):
                raise
            await asyncio.sleep(0.025)
            continue
        if client.protocol_version != PROTOCOL or client.server_capabilities.tools is None:
            raise CheckError("incompatible MCP discovery response")
        return client
    raise CheckError("MCP child exited before discovery")


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            async with asyncio.timeout(3):
                await process.wait()
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()


@asynccontextmanager
async def _mcp(config: Path):
    executable = Path(sysconfig.get_path("scripts")).absolute() / "mcp-core"
    deadline = asyncio.get_running_loop().time() + 10
    started = asyncio.create_task(asyncio.create_subprocess_exec(
        str(executable), "--config", str(config), "--port", "0",
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, limit=4096,
    ))
    stack = AsyncExitStack()
    try:
        try:
            async with asyncio.timeout_at(deadline):
                process = await asyncio.shield(started)
                url = _read_url(await process.stdout.readline())
                http = await stack.enter_async_context(httpx2.AsyncClient(
                    trust_env=False, follow_redirects=False, event_hooks={"response": [_reject_redirect]},
                ))
                client = await _discover(stack, http, url, process)
        except CheckError:
            raise
        except Exception:
            raise CheckError("MCP startup or discovery failed") from None
        yield client, url
    finally:
        try:
            await stack.aclose()
        finally:
            with suppress(OSError):
                await _stop(await started)


async def _call_demo(client: Client, arguments: dict) -> dict:
    result = await client.call_tool(DEMO_TOOL, arguments)
    if result.is_error or result.structured_content != DEMO_RESULT:
        raise CheckError("unexpected demo_echo result")
    return result.structured_content


async def _check_core(empty: Path, demo: Path) -> None:
    async with _mcp(empty) as (client, empty_url), asyncio.timeout(15):
        if (await client.list_tools()).tools:
            raise CheckError("empty configuration returned tools")
    async with _mcp(demo) as (client, demo_url), asyncio.timeout(15):
        if [tool.name for tool in (await client.list_tools()).tools] != [DEMO_TOOL]:
            raise CheckError("unexpected demo tools catalog")
        result = await _call_demo(client, DEMO_ARGUMENTS)
    print(json.dumps({
        "check": "core", "protocol": PROTOCOL, "empty_url": empty_url, "demo_url": demo_url,
        "empty_tools": [], "tool": DEMO_TOOL, "result": result,
    }))


async def _model_request(http: httpx2.AsyncClient, parameters: dict, messages: list, choice: str) -> dict:
    try:
        async with asyncio.timeout(120):
            response = await http.post(MODEL_URL, json={
                **parameters, "messages": messages, "tool_choice": choice,
            })
            response.raise_for_status()
            message = response.json()["choices"][0]["message"]
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ValueError("unexpected model message")
        return message
    except (httpx2.HTTPError, TimeoutError, ValueError, TypeError, KeyError, IndexError):
        raise CheckError(f"model request failed (tool_choice={choice})") from None


def _read_tool_call(message: dict) -> tuple[str, dict]:
    try:
        [call] = message["tool_calls"]
        if call["type"] != "function" or call["function"]["name"] != DEMO_TOOL:
            raise ValueError("unexpected function")
        call_id = call["id"]
        arguments = json.loads(call["function"]["arguments"])
        if not isinstance(call_id, str) or not call_id.strip() or arguments != DEMO_ARGUMENTS:
            raise ValueError("unexpected arguments or call ID")
        return call_id, arguments
    except (KeyError, TypeError, ValueError):
        raise CheckError("unexpected model tool call") from None


async def _check_e2e(config: Path) -> None:
    async with (
        _mcp(config) as (client, mcp_url),
        httpx2.AsyncClient(trust_env=False, follow_redirects=False, timeout=120) as http,
    ):
        async with asyncio.timeout(15):
            listing = await client.list_tools()
            tool = next((tool for tool in listing.tools if tool.name == DEMO_TOOL), None)
        if tool is None:
            raise CheckError("demo_echo is absent from MCP tools/list")
        parameters = {
            "model": "qwen3-4b", "stream": False, "temperature": 0, "seed": 42, "max_tokens": 128,
            "tools": [{
                "type": "function",
                "function": {"name": tool.name, "description": tool.description, "parameters": tool.input_schema},
            }],
        }
        user = {"role": "user", "content": 'Call demo_echo once with text "mcp-check-001", then report its result.'}
        assistant = await _model_request(http, parameters, [user], "required")
        call_id, arguments = _read_tool_call(assistant)
        async with asyncio.timeout(15):
            result = await _call_demo(client, arguments)
        tool_message = {"role": "tool", "tool_call_id": call_id, "content": json.dumps(result)}
        final = await _model_request(http, parameters, [user, assistant, tool_message], "none")
        if not isinstance(final.get("content"), str) or not final["content"].strip():
            raise CheckError("model final answer is empty")
    print(json.dumps({
        "check": "e2e", "protocol": PROTOCOL, "mcp_url": mcp_url, "model_url": MODEL_URL,
        "tool": tool.name, "arguments": arguments, "result": result,
        "sent_tool_message": tool_message, "final_answer": final["content"],
    }))


def main() -> int:
    parser = argparse.ArgumentParser(prog="mcp-core-check")
    parser.add_argument("--version", action="version", version=version("mcp-offline-core"))
    commands = parser.add_subparsers(dest="command", required=True)
    core = commands.add_parser("core")
    core.add_argument("--empty-config", type=Path, required=True)
    core.add_argument("--demo-config", type=Path, required=True)
    e2e = commands.add_parser("e2e")
    e2e.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        if args.command == "core":
            asyncio.run(_check_core(args.empty_config, args.demo_config))
        else:
            asyncio.run(_check_e2e(args.config))
    except CheckError as error:
        print(f"mcp-core-check: {error}", file=sys.stderr)
        return 1
    except Exception:
        print("mcp-core-check: MCP connection or tool operation failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
