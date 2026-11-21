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

## 1. Найдите исходники Python-пакета

Готовый пакет `mcp-zip-tools` расположен в каталоге `tools/zip/`.

```text
tools/zip/
├── pyproject.toml
├── mcp_zip_tools.py
├── zip-module.json
└── tests/
```

В `pyproject.toml` указаны имя пакета и один Python-модуль. При выполнении
инструмент использует только стандартную библиотеку.

[Исходный pyproject.toml](../../tools/zip/pyproject.toml)

## 2. Разберите обработчик

Ядро вызывает синхронную функцию `extract_zip(arguments, context)`.
Она получает проверенный объект аргументов и неизменяемое множество
`context.grants`. Проверку полномочия ставим перед обращением к файлам.

[Исходный код обработчика](../../tools/zip/mcp_zip_tools.py)

Пути внутри ZIP могут содержать вложенные каталоги. Обработчик отклоняет
абсолютные пути, компоненты `..`, ссылки, специальные файлы и повторяющиеся
имена. Он ограничивает вход 8 МиБ, число записей 200 и суммарный объём
записи на диск 32 МиБ за один вызов. Каталог `files` появляется после
успешной распаковки всех записей. При принудительной остановке процесса
по таймауту может остаться временный каталог `.staging`. Проверяйте
такие каталоги после остановки экземпляра сервера.

## 3. Проверьте манифест

В `zip-module.json` объявлен один инструмент. Поле `handler` связывает
импортируемый модуль `mcp_zip_tools` с функцией `extract_zip`.
Схема принимает только одно поле `archive`.

[Готовый JSON-манифест](../../tools/zip/zip-module.json)

## 4. Соберите wheel и установите его в окружение MCP

На машине подготовки с Python 3.13 и `pip` выполните из корня
репозитория. Для каждой сборки используйте свободный каталог для wheel
и манифеста.

```bash
(
  set -eu
  cd tools/zip
  mkdir -- dist
  python3 -m pip wheel --no-deps --wheel-dir dist .
  cp zip-module.json dist/
  cd dist
  sha256sum mcp_zip_tools-0.1.0-py3-none-any.whl zip-module.json > SHA256SUMS
)
```

Перенесите содержимое `tools/zip/dist/` на офлайн-машину, например в
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

Для проверки установленного пакета запустите из корня репозитория
`"$MCP_RUNTIME/venv/bin/python" -m unittest discover -s tools/zip/tests -v`.
[Тесты ZIP-инструмента](../../tools/zip/tests/test_mcp_zip_tools.py)
