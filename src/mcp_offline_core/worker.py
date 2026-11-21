"""Execute one trusted tool with a private response pipe."""

from dataclasses import dataclass
from importlib import import_module
import json
import os
import sys


MAX_MESSAGE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class InvocationContext:
    grants: frozenset[str]


def encode_message(value: dict) -> bytes:
    payload = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError("message exceeds the internal limit")
    return payload


def _invoke() -> bytes:
    try:
        payload = sys.stdin.buffer.read(MAX_MESSAGE_BYTES + 1)
        if len(payload) > MAX_MESSAGE_BYTES:
            raise ValueError("request exceeds the internal limit")
        request = json.loads(payload)
        module, _, name = request["handler"].partition(":")
        handler = getattr(import_module(module), name)
        context = InvocationContext(frozenset(request["grants"]))
        try:
            result = handler(request["arguments"], context)
        except PermissionError:
            return encode_message({"error": "permission_denied"})
        if not isinstance(result, dict):
            raise ValueError("handler must return a JSON object")
        return encode_message({"result": result})
    except Exception:
        return encode_message({"error": "handler_error"})


def main() -> None:
    with os.fdopen(os.dup(1), "wb") as response:
        with open(os.devnull, "wb") as null:
            os.dup2(null.fileno(), 1)
            os.dup2(null.fileno(), 2)
        response.write(_invoke() + b"\n")


if __name__ == "__main__":
    main()
