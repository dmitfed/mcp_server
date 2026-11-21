# Как добавить инструмент MCP на примере распаковки ZIP

Инструмент состоит из обычной Python-функции, манифеста и записи в конфигурации
сервера. Функция устанавливается в то же виртуальное окружение, что и `mcp-core`.
Манифест даёт серверу имя, описание и схему аргументов для `tools/list`.
При `tools/call` ядро проверяет аргументы и запускает функцию в отдельном
процессе. Этот же порядок подходит для будущих инструментов, включая RAG.
[Контракт модуля v1](contracts/module-v1.md)

В примере клиент передаёт `{"archive":"sample.zip"}`. Сервер читает файл
из выделенного каталога `inbox/`, распаковывает его в новую папку внутри
`output/` и возвращает объект с относительным каталогом и списком файлов.
Куда положить архив и где хранить результаты, задаёт оператор.

## 1. Создайте отдельный Python-пакет

В отдельной рабочей папке `mcp-zip-tool/` создайте три файла.

```text
mcp-zip-tool/
├── pyproject.toml
├── mcp_zip_tools.py
└── zip-module.json
```

В `pyproject.toml` укажите имя пакета и один Python-модуль. При выполнении
инструмент использует только стандартную библиотеку.

```toml
[build-system]
requires = ["setuptools>=77"]
build-backend = "setuptools.build_meta"

[project]
name = "mcp-zip-tools"
version = "0.1.0"
requires-python = ">=3.13"

[tool.setuptools]
py-modules = ["mcp_zip_tools"]
```

## 2. Напишите обработчик

Ядро вызывает синхронную функцию `extract_zip(arguments, context)`.
Она получает проверенный объект аргументов и неизменяемое множество
`context.grants`. Проверку полномочия ставим перед обращением к файлам.

В `mcp_zip_tools.py` запишите следующий код.

```python
import os
from pathlib import Path
import re
import shutil
import stat
from tempfile import mkdtemp
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo


MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 200


def _entry_path(entry: ZipInfo) -> str:
    """Проверить имя и тип записи до создания файла или каталога."""
    name = entry.orig_filename
    if len(name.encode("utf-8")) > 240 or any(char in name for char in ("\\", ":", "\x00")):
        raise ValueError("Недопустимое имя записи ZIP")
    relative = name.removesuffix("/")
    if any(part in ("", ".", "..") for part in relative.split("/")):
        raise ValueError("Недопустимый путь в ZIP")
    mode = stat.S_IFMT(entry.external_attr >> 16)
    expected = stat.S_IFDIR if entry.is_dir() else stat.S_IFREG
    if mode not in (0, expected):
        raise ValueError("ZIP содержит ссылку или специальный файл")
    if entry.compress_type not in (ZIP_STORED, ZIP_DEFLATED):
        raise ValueError("Неподдерживаемый метод сжатия")
    return relative


def extract_zip(arguments, context):
    """Распаковать один ZIP и вернуть список обычных файлов."""
    if "zip.extract" not in context.grants:
        raise PermissionError("Требуется полномочие zip.extract")
    name = arguments["archive"]
    if re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,100}\.zip", name) is None:
        raise ValueError("Нужно имя файла ZIP из каталога inbox")

    root = Path(os.environ["MCP_ZIP_ROOT"]).resolve(strict=True)
    output = root / "output"
    archive_fd = os.open(root / "inbox" / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(archive_fd, "rb") as source:
        source_info = os.fstat(source.fileno())
        if not stat.S_ISREG(source_info.st_mode) or source_info.st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("Требуется обычный ZIP-файл размером до 8 МиБ")
        with ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ENTRIES or sum(entry.file_size for entry in entries) > MAX_OUTPUT_BYTES:
                raise ValueError("Превышен лимит распаковки")

            run = Path(mkdtemp(prefix="zip-", dir=output))
            staging = run / ".staging"
            files = []
            seen = set()
            written = 0
            try:
                staging.mkdir()
                for entry in entries:
                    relative = _entry_path(entry)
                    if relative in seen:
                        raise ValueError("Повторяющийся путь в ZIP")
                    seen.add(relative)
                    target = staging / relative
                    if entry.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue

                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(entry) as member, target.open("xb") as destination:
                        while chunk := member.read(64 * 1024):
                            written += len(chunk)
                            if written > MAX_OUTPUT_BYTES:
                                raise ValueError("Превышен лимит распакованных байтов")
                            destination.write(chunk)
                    files.append(relative)
                staging.rename(run / "files")
            except BaseException:
                shutil.rmtree(run, ignore_errors=True)
                raise
    return {"directory": f"output/{run.name}/files", "files": sorted(files)}
```

Пути внутри ZIP могут содержать вложенные каталоги. Обработчик отклоняет
абсолютные пути, компоненты `..`, ссылки, специальные файлы и повторяющиеся
имена. Он ограничивает вход 8 МиБ, число записей 200 и суммарный объём
записи на диск 32 МиБ за один вызов. Каталог `files` появляется после
успешной распаковки всех записей. При принудительной остановке процесса
по таймауту может остаться временный каталог `.staging`. Проверяйте
такие каталоги после остановки экземпляра сервера.

## 3. Опишите инструмент в манифесте

В `zip-module.json` объявите один инструмент. Поле `handler` связывает
импортируемый модуль `mcp_zip_tools` с функцией `extract_zip`.
Схема принимает только одно поле `archive`.

```json
{
  "contract_version": 1,
  "module_id": "zip_tools",
  "tools": [
    {
      "name": "zip_extract",
      "description": "Распаковать локальный ZIP из inbox и вернуть список файлов.",
      "handler": "mcp_zip_tools:extract_zip",
      "input_schema": {
        "type": "object",
        "properties": {
          "archive": {
            "type": "string",
            "maxLength": 105,
            "pattern": "^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,100}\\.zip$",
            "description": "Имя ZIP в inbox, например sample.zip."
          }
        },
        "required": ["archive"],
        "additionalProperties": false
      }
    }
  ]
}
```

## 4. Соберите wheel и установите его в окружение MCP

На машине подготовки запустите из каталога `mcp-zip-tool/`.

```bash
python3 -m pip wheel --no-deps --wheel-dir dist .
cp zip-module.json dist/
(
  cd dist
  sha256sum mcp_zip_tools-0.1.0-py3-none-any.whl zip-module.json > SHA256SUMS
)
```

Перенесите содержимое `dist/` на офлайн-машину, например в
`$HOME/mcp-offline/mcp-zip-tool/`. Ниже предполагается, что ядро уже
установлено. Если использовали другие пути, подставьте их.
[Установка ядра](deployment.md).

```bash
MCP_RUNTIME="$HOME/mcp-offline/mcp-core-runtime"
MCP_PLUGIN="$HOME/mcp-offline/mcp-zip-tool"
(
  set -eu
  cd "$MCP_PLUGIN"
  sha256sum --check SHA256SUMS
  "$MCP_RUNTIME/venv/bin/python" -m pip --isolated install --no-index --no-deps \
    "$MCP_PLUGIN/mcp_zip_tools-0.1.0-py3-none-any.whl"
  mkdir -p "$MCP_RUNTIME/config"
  cp zip-module.json "$MCP_RUNTIME/config/"
)
```

## 5. Подключите модуль и запустите сервер

Создайте `$MCP_RUNTIME/config/zip-tools.toml`. Путь `manifest`
отсчитывается от каталога TOML. Полномочия задаёт оператор экземпляра,
и они действуют для его локальных MCP-клиентов.

```toml
config_version = 1
grants = ["zip.extract"]

[[modules]]
manifest = "zip-module.json"
```

Чтобы добавить инструмент к уже настроенному экземпляру, внесите
полномочие в существующий верхнеуровневый список `grants`, добавьте
ещё один блок `[[modules]]` и перезапустите сервер. Для пустого профиля
с `modules = []` замените эту строку блоком `[[modules]]`.

В оболочке запуска задайте каталог данных и создайте `inbox/` и
`output/`. Держите эти каталоги под контролем пользователя сервера.
Дочерний процесс наследует `MCP_ZIP_ROOT`.

```bash
export MCP_ZIP_ROOT="$MCP_RUNTIME/zip-data"
install -d -m 0700 "$MCP_ZIP_ROOT" "$MCP_ZIP_ROOT/inbox" "$MCP_ZIP_ROOT/output"
"$MCP_RUNTIME/venv/bin/mcp-core" --config "$MCP_RUNTIME/config/zip-tools.toml" --port 8765
```

## 6. Проверьте каталог и распаковку по MCP

В другой оболочке создайте небольшой тестовый ZIP. Модель для проверки
инструмента не требуется.

```bash
MCP_RUNTIME="$HOME/mcp-offline/mcp-core-runtime"
"$MCP_RUNTIME/venv/bin/python" - "$MCP_RUNTIME/zip-data/inbox/sample.zip" <<'PY'
import sys
from zipfile import ZipFile

with ZipFile(sys.argv[1], "x") as archive:
    archive.writestr("readme.txt", "Проверка MCP\n")
    archive.writestr("docs/notes.txt", "Вложенный файл\n")
PY
```

Подключитесь локальным MCP-клиентом по Streamable HTTP к
`http://127.0.0.1:8765/mcp`. В его каталоге должен появиться `zip_extract`.
Через установленный SDK эти же шаги выглядят так.

```bash
"$MCP_RUNTIME/venv/bin/python" - <<'PY'
import asyncio
import json

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


async def main():
    url = "http://127.0.0.1:8765/mcp"
    async with asyncio.timeout(15), httpx2.AsyncClient(trust_env=False) as http:
        async with Client(streamable_http_client(url, http_client=http), cache=None) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            if "zip_extract" not in names:
                raise SystemExit("В каталоге нет zip_extract")
            result = await client.call_tool("zip_extract", {"archive": "sample.zip"})
            if result.is_error:
                raise SystemExit(result.content[0].text)
            print(json.dumps(result.structured_content, ensure_ascii=False, indent=2))


asyncio.run(main())
PY
```

Ожидаемый результат. Имя сгенерированной папки у каждого вызова своё.

```json
{
  "directory": "output/zip-abc12345/files",
  "files": ["docs/notes.txt", "readme.txt"]
}
```

Путь `directory` отсчитывается от `$MCP_RUNTIME/zip-data`. Клиент
получает имена файлов, а сами файлы остаются на диске. Если инструмент
появился в каталоге, но вызов завершился с `handler_error`, проверьте
установку wheel в нужный `venv`, переменную `MCP_ZIP_ROOT`, доступ к
папкам и корректность ZIP. При отсутствии `zip.extract` ядро фиксирует
`permission_denied` в stderr. Длительность вызова ограничена
`execution.timeout_seconds` в конфигурации сервера.
