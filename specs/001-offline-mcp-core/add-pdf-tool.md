# Как добавить инструмент MCP на примере инструмента чтения PDF

В этом примере `pdf_read` извлекает текст из локального PDF с помощью
`pypdf`. Клиент передаёт имя файла и диапазон страниц, а сервер возвращает
текст по страницам. Файл лежит в каталоге, который выбирает оператор.
Для подключения достаточно Python-пакета, JSON-манифеста и записи в TOML.
[Контракт модуля v1](contracts/module-v1.md)

Обработчик запускается тем же Python, что и `mcp-core`. Поэтому wheel
инструмента и его зависимости установим в общее виртуальное окружение.
Подготовим отдельный комплект на подключённой машине и проверим его на
Debian 13 amd64 с CPython 3.13 без внешней сети.
[Развёртывание ядра](deployment.md)

## 1. Найдите исходники Python-пакета и его зависимости

Готовый пакет `mcp-pdf-tool` расположен в каталоге `tools/pdf/`.

```text
tools/pdf/
├── pyproject.toml
├── mcp_pdf_tool.py
├── pdf-module.json
└── tests/
```

В `pyproject.toml` зависимость `pypdf` указана в `[project].dependencies`. Эта
запись попадёт в метаданные wheel, и `pip` разрешит зависимость при сборке
комплекта. `setuptools` нужен на этапе сборки, а точная версия
`pypdf` для развёртывания попадёт в `requirements.lock`.
[Описание выпуска pypdf 7.1.1](https://pypi.org/project/pypdf/7.1.1/)

[Исходный pyproject.toml](../../tools/pdf/pyproject.toml)

## 2. Разберите обработчик

Ядро передаёт функции `read_pdf` проверенные аргументы и контекст
с полномочиями экземпляра. Обработчик проверяет
`pdf.read` перед открытием файла.

[Исходный код обработчика](../../tools/pdf/mcp_pdf_tool.py)

PDF открывается только на чтение. Абсолютные пути, вложенные пути и
символические ссылки на документы отклоняются. Используйте выделенный
каталог, доступный на запись только оператору или пользователю сервера.
При добавлении PDF завершите копирование файла до передачи имени клиенту.

Лимиты примера составляют 8 МиБ на файл, пять страниц на вызов и
4 000 символов текста на страницу. `truncated` показывает, что текст
страницы обрезан, а `next_page` помогает продолжить со следующей страницы.
Ограничение символов применяется после извлечения текста. При разборе
страницы `pypdf` может выделить больше памяти, а дедлайн вызова задаётся
в настройках экземпляра MCP.

Инструмент читает текстовый слой PDF. У скана без текстового слоя
`text` может быть пустым. Распознавание изображений требует отдельного
OCR-инструмента. [Извлечение текста в pypdf](https://pypdf.readthedocs.io/en/latest/user/extract-text.html)

## 3. Проверьте JSON-манифест

В `pdf-module.json` указаны имя инструмента и ссылка на обработчик
в формате `модуль:функция`. Нумерация страниц для клиента начинается с 1.
Значения `default` описывают поведение для клиента, а обработчик сам
применяет их для пропущенных аргументов.

[Готовый JSON-манифест](../../tools/pdf/pdf-module.json)

## 4. Соберите и перенесите комплект с зависимостями

На подключённой машине с Debian 13 amd64, CPython 3.13 и `python3-pip`
возьмите wheelhouse уже принятого `offline-kit/`.
Из корня репозитория запустите сборку пакета из `tools/pdf/`.
Команда передаёт существующие wheels ядра как исходные требования, поэтому
конфликт версий обнаружится при подборе зависимостей модуля. Используйте
новый каталог назначения для каждого комплекта.
[Сборщик и функция `write_lock`](../../scripts/build-offline-kit.py)

```bash
MCP_REPO="$PWD"
MCP_PDF_BUNDLE="$HOME/mcp-pdf-bundle"

(
  set -eu
  mkdir -- "$MCP_PDF_BUNDLE"
  python3 -m pip wheel --only-binary=:all: \
    --wheel-dir "$MCP_PDF_BUNDLE/wheelhouse" \
    "$MCP_REPO"/offline-kit/wheelhouse/*.whl "$MCP_REPO/tools/pdf"

  python3 - "$MCP_REPO" "$MCP_PDF_BUNDLE" <<'PY'
from pathlib import Path
from runpy import run_path
import sys

repo, bundle = map(Path, sys.argv[1:])
write_lock = run_path(str(repo / "scripts/build-offline-kit.py"))["write_lock"]
write_lock(bundle / "wheelhouse", bundle / "requirements.lock")
PY

  cp "$MCP_REPO/tools/pdf/pdf-module.json" "$MCP_PDF_BUNDLE/"
  cd "$MCP_PDF_BUNDLE"
  sha256sum requirements.lock pdf-module.json wheelhouse/*.whl > SHA256SUMS
)
```

Перенесите весь `mcp-pdf-bundle/` на офлайн-машину. В примере новое
окружение создаётся рядом с комплектом, а предыдущее окружение ядра
можно использовать до проверки нового инструмента.

```bash
MCP_PDF_BUNDLE="$HOME/mcp-pdf-bundle"
MCP_PDF_RUNTIME="$HOME/mcp-pdf-runtime"

(
  set -eu
  cd "$MCP_PDF_BUNDLE"
  sha256sum --check SHA256SUMS
  mkdir -- "$MCP_PDF_RUNTIME"
  python3 -m venv "$MCP_PDF_RUNTIME/venv"
  "$MCP_PDF_RUNTIME/venv/bin/python" -m pip --isolated install \
    --no-index --no-cache-dir --find-links "$MCP_PDF_BUNDLE/wheelhouse" \
    --only-binary=:all: --require-hashes -r requirements.lock
  "$MCP_PDF_RUNTIME/venv/bin/python" -m pip check
  mkdir -p "$MCP_PDF_RUNTIME/config"
  cp pdf-module.json "$MCP_PDF_RUNTIME/config/"
)
```

## 5. Подключите модуль к серверу

Создайте `$MCP_PDF_RUNTIME/config/pdf-tools.toml`. Относительный путь
`manifest` указывает на файл рядом с TOML. Полномочие `pdf.read`
получают локальные клиенты этого экземпляра.

```toml
config_version = 1
grants = ["pdf.read"]

[[modules]]
manifest = "pdf-module.json"
```

Чтобы дополнить существующий конфиг, добавьте полномочие в верхнеуровневое
поле `grants` и внесите отдельный блок `[[modules]]`. Если пустой конфиг
содержит `modules = []`, замените эту строку блоком. Для загрузки
нового манифеста перезапустите сервер из нового `venv`.

Для отдельного экземпляра в примере используется порт `8766`. В первой
оболочке создайте каталог документов, задайте путь для дочерних процессов
и запустите сервер.

```bash
MCP_PDF_RUNTIME="$HOME/mcp-pdf-runtime"
export MCP_PDF_ROOT="$MCP_PDF_RUNTIME/documents"
install -d -m 0700 "$MCP_PDF_ROOT"
"$MCP_PDF_RUNTIME/venv/bin/mcp-core" \
  --config "$MCP_PDF_RUNTIME/config/pdf-tools.toml" --port 8766
```

## 6. Проверьте чтение текста через MCP

Во второй оболочке положите небольшой PDF с текстовым слоем в каталог
документов под именем `sample.pdf`. Для воспроизводимой проверки можно
создать двухстраничный PDF тем же установленным `pypdf`.

```bash
MCP_PDF_RUNTIME="$HOME/mcp-pdf-runtime"
"$MCP_PDF_RUNTIME/venv/bin/python" - "$MCP_PDF_RUNTIME/documents/sample.pdf" <<'PY'
import sys

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


writer = PdfWriter()
for text in (b"Offline MCP PDF example", b"Second page"):
    page = writer.add_blank_page(width=300, height=200)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }),
        }),
    })
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 14 Tf 20 150 Td (" + text + b") Tj ET")
    page.replace_contents(content)

with open(sys.argv[1], "xb") as output:
    writer.write(output)
PY
```

На `http://127.0.0.1:8766/mcp` проверьте каталог и вызов инструмента.
Этот код запрашивает только первую страницу. LLM для проверки не нужен.

```bash
"$MCP_PDF_RUNTIME/venv/bin/python" - <<'PY'
import asyncio
import json

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


async def main():
    url = "http://127.0.0.1:8766/mcp"
    async with asyncio.timeout(15), httpx2.AsyncClient(trust_env=False) as http:
        async with Client(streamable_http_client(url, http_client=http), cache=None) as client:
            if "pdf_read" not in {tool.name for tool in (await client.list_tools()).tools}:
                raise SystemExit("В каталоге нет pdf_read")
            result = await client.call_tool("pdf_read", {
                "file": "sample.pdf",
                "start_page": 1,
                "max_pages": 1,
            })
            if result.is_error:
                raise SystemExit(result.content[0].text)
            print(json.dumps(result.structured_content, ensure_ascii=False, indent=2))


asyncio.run(main())
PY
```

Для PDF из примера ожидается такой результат.

```json
{
  "file": "sample.pdf",
  "page_count": 2,
  "pages": [
    {"page": 1, "text": "Offline MCP PDF example", "truncated": false}
  ],
  "next_page": 2
}
```

Следующий запрос `{"file":"sample.pdf","start_page":2,"max_pages":1}`
вернёт `Second page`, а `next_page` будет `null`.

Если `pdf_read` есть в каталоге, но возвращает ошибку, проверьте код в
stderr сервера. При `permission_denied` проверьте поле `grants` и права
на документы. При `handler_error` проверьте импорт `pypdf` из
нового окружения, переменную `MCP_PDF_ROOT`, размер файла и наличие
шифрования. Код `tool_timeout` указывает на истечение дедлайна вызова.

Для проверки установленного пакета запустите из корня репозитория
`"$MCP_PDF_RUNTIME/venv/bin/python" -m unittest discover -s tools/pdf/tests -v`.
[Тесты PDF-инструмента](../../tools/pdf/tests/test_mcp_pdf_tool.py)
