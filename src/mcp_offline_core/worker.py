"""Выполнение одного вызова доверенного обработчика в дочернем процессе."""

from dataclasses import dataclass
from importlib import import_module
import json
import os
import sys


MAX_MESSAGE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class InvocationContext:
    """Неизменяемые полномочия, переданные из конфигурации сервера."""

    grants: frozenset[str]


def encode_message(value: dict) -> bytes:
    """Сериализовать объект для внутреннего канала в компактный JSON.

    Лимит 1 МиБ применяется к байтам JSON. NaN и бесконечности запрещены,
    чтобы результат можно было передать как стандартный JSON.
    """
    payload = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError("message exceeds the internal limit")
    return payload


def _invoke() -> bytes:
    """Прочитать один запрос, импортировать обработчик и выполнить его синхронно.

    Обработчик получает уже проверенные аргументы и контекст. Возвращаемый
    словарь упаковывается в поле `result`. Отказ в полномочиях и остальные
    сбои кодируются без исходного текста исключения.
    """
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
    """Выделить канал ответа и перенаправить вывод модуля до его импорта.

    Копия исходного stdout сохраняет канал с родительским процессом.
    Обычные stdout и stderr перенаправляются в `/dev/null`, чтобы вывод
    обработчика не смешивался с JSON. Перевод строки завершает ответ.
    """
    with os.fdopen(os.dup(1), "wb") as response:
        with open(os.devnull, "wb") as null:
            os.dup2(null.fileno(), 1)
            os.dup2(null.fileno(), 2)
        response.write(_invoke() + b"\n")


if __name__ == "__main__":
    main()
