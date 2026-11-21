"""HTTP-граница Streamable HTTP и команда запуска `mcp-core`."""

import argparse
import asyncio
from datetime import UTC, datetime
from importlib.metadata import version
import json
import logging
from pathlib import Path
import socket
import sys

from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool
import uvicorn

from .execution import Executor
from .registry import Configuration, ConfigurationError, RegisteredTool, load_configuration, load_registry


PACKAGE = "mcp-offline-core"
LOOPBACK = "127.0.0.1"
TOOL_ERRORS = {
    "invalid_arguments": "Invalid tool arguments",
    "permission_denied": "Tool permission denied",
    "handler_error": "Tool handler failed",
    "tool_timeout": "Tool execution timed out",
    "busy": "All tool execution slots are busy",
    "rate_limited": "Tool call rate limit exceeded",
}


def make_http_app(server: Server, transport_security: TransportSecuritySettings):
    """Создать ASGI-приложение SDK для MCP по пути `/mcp`.

    Режим без серверных сессий подходит проверочному хосту, а JSON-ответы
    позволяют ему получать результат вызова одним JSON-документом.
    """
    return server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=transport_security,
    )


def _request_boundary(app, allowed_hosts: set[bytes]):
    """Обернуть приложение проверкой HTTP-заголовков до разбора MCP-запроса."""

    async def guarded(scope, receive, send):
        """Пропустить служебные события ASGI и отфильтровать HTTP-запросы.

        Для HTTP требуется ровно один разрешённый `Host`. Наличие `Origin`,
        в том числе с пустым значением, приводит к ответу 403. При отказе
        тело запроса не читается.
        """
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        headers = scope["headers"]
        hosts = [value for name, value in headers if name == b"host"]
        if len(hosts) != 1 or hosts[0] not in allowed_hosts:
            status = 421
        elif any(name == b"origin" for name, _ in headers):
            status = 403
        else:
            await app(scope, receive, send)
            return
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})

    return guarded


def _call_record(name: str | None, start: float, code: str) -> None:
    """Записать одну JSON-строку о вызове в stderr.

    Время события берётся по UTC, а длительность по монотонным часам цикла.
    Вызывающий обработчик передаёт только зарегистрированное имя и фиксированный
    код, чтобы в журнал не попали аргументы, результат или текст исключения.
    """
    record = {
        "time": datetime.now(UTC).isoformat(),
        "duration_ms": round((asyncio.get_running_loop().time() - start) * 1000, 3),
        "status": "ok" if code == "ok" else "error",
        "code": code,
    }
    if name is not None:
        record["tool"] = name
    print(json.dumps(record), file=sys.stderr, flush=True)


def _make_server(config: Configuration, registry: dict[str, RegisteredTool], executor: Executor) -> Server:
    """Связать MCP SDK с каталогом и диспетчером вызовов одного экземпляра.

    Каталог формируется один раз из проверенного реестра. Сортировка фиксирует
    порядок публикации, а исключение отключённых имён действует и для вызовов.
    """
    enabled = {name: registry[name] for name in sorted(registry) if name not in config.disabled_tools}

    async def list_tools(_context, _params):
        """Опубликовать имена, описания и входные схемы доступных инструментов."""
        return ListToolsResult(tools=[
            Tool(name=tool.name, description=tool.description, inputSchema=tool.schema)
            for tool in enabled.values()
        ])

    def input_schema(name):
        """Передать SDK схему доступного инструмента для проверки заголовков MCP."""
        tool = enabled.get(name)
        return tool.schema if tool is not None else None

    async def call_tool(_context, params):
        """Проверить доступность инструмента и аргументы, затем выполнить вызов.

        Неизвестное или отключённое имя даёт ошибку JSON-RPC. Нарушение
        схемы и ошибки исполнения возвращаются результатом MCP с `isError`.
        Успешный объект передаётся клиенту и как структура, и как JSON-текст.
        """
        start = asyncio.get_running_loop().time()
        tool = enabled.get(params.name)
        if tool is None:
            registered_name = params.name if params.name in registry else None
            _call_record(registered_name, start, "unknown_tool")
            raise MCPError(code=-32602, message="Unknown or disabled tool")

        arguments = params.arguments if params.arguments is not None else {}
        try:
            valid = tool.validator.is_valid(arguments)
        except (ValueError, RecursionError):
            valid = False
        if valid:
            # Полномочия берутся из конфигурации оператора, аргументы приходят от клиента.
            result = await executor.call(tool.handler, arguments, config.grants)
        else:
            result = "invalid_arguments"
        if isinstance(result, str):
            _call_record(tool.name, start, result)
            return CallToolResult(content=[TextContent(type="text", text=TOOL_ERRORS[result])], isError=True)

        _call_record(tool.name, start, "ok")
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=True, allow_nan=False))],
            structuredContent=result,
        )

    return Server(
        PACKAGE, version=version(PACKAGE), on_list_tools=list_tools,
        get_tool_input_schema=input_schema, on_call_tool=call_tool,
    )


async def _serve(listener: socket.socket, config: Configuration, registry: dict[str, RegisteredTool]) -> int:
    """Запустить Uvicorn на уже привязанном сокете и дождаться освобождения исполнителей.

    Допустимый `Host` строится из фактического порта, выделенного сокету.
    Запись `mcp_url` сообщает адрес клиенту. Готовность приложения клиент
    подтверждает отдельным запросом discovery.
    """
    port = listener.getsockname()[1]
    address = f"{LOOPBACK}:{port}"
    hosts = [address, LOOPBACK] if port == 80 else [address]
    security = TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=[])
    executor = Executor(config.timeout_seconds, config.max_concurrent_calls)
    server = _make_server(config, registry, executor)
    app = _request_boundary(make_http_app(server, security), {host.encode() for host in hosts})
    http_config = uvicorn.Config(
        app, proxy_headers=False, access_log=False, log_config=None, lifespan="on", timeout_graceful_shutdown=2,
    )
    runner = uvicorn.Server(http_config)
    print(json.dumps({"mcp_url": f"http://{address}/mcp"}), flush=True)
    try:
        await runner.serve(sockets=[listener])
    finally:
        await executor.aclose()
    if not runner.started:
        print("MCP HTTP server startup failed", file=sys.stderr)
        return 1
    return 0


def _tcp_port(value: str) -> int:
    """Преобразовать параметр CLI в TCP-порт, сохранив 0 для выбора порта ОС."""
    port = int(value)
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return port


def main() -> int:
    """Прочитать параметры CLI, загрузить реестр и запустить HTTP-сервер.

    Конфигурация целиком проверяется до привязки IPv4-сокета к loopback.
    Ошибка конфигурации или привязки даёт сообщение в stderr и код выхода 1.
    """
    parser = argparse.ArgumentParser(prog="mcp-core")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=_tcp_port, default=8765)
    parser.add_argument("--version", action="version", version=version(PACKAGE))
    arguments = parser.parse_args()
    # Подавляем журналы библиотек, оставляя фиксированные сообщения CLI и записи о вызовах.
    logging.disable(logging.CRITICAL)
    try:
        config = load_configuration(arguments.config)
        registry = load_registry(config)
    except ConfigurationError as error:
        print(f"mcp-core: {error}", file=sys.stderr)
        return 1

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((LOOPBACK, arguments.port))
        except OSError:
            print(f"mcp-core: cannot bind {LOOPBACK}:{arguments.port}", file=sys.stderr)
            return 1
        try:
            return asyncio.run(_serve(listener, config, registry))
        except KeyboardInterrupt:
            return 0
