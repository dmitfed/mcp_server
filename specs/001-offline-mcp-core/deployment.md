# Развёртывание MCP-сервера на офлайн-машине

Сервер `mcp-core` работает на Debian 13 amd64 с Python 3.13 или новее.
Примеры ниже рассчитаны на Bash и системный CPython 3.13, для которого собран
проверенный комплект версии `0.1.0`. MCP-хост и сервер работают на одной машине
и обмениваются запросами через Streamable HTTP по адресу `127.0.0.1`.
Для обычного запуска достаточно шагов 1–5. Шаг 7 нужен для проверки с моделью.

## 1. Подготовьте Debian и переносимый комплект

В базовом образе нужны `python3`, `python3-venv`, `bash`, `coreutils`,
`curl` и `iproute2`. Для профиля с моделью также нужны `tar`, `gzip`,
`libc-bin` и библиотеки `libgomp1`, `libstdc++6`, `libgcc-s1`,
`libssl3t64`, `zlib1g`, `libzstd1` вместе с зависимостями Debian.
Подготовьте системные пакеты в базовом образе до переноса на изолированную
машину. Если целевая ОС уже изолирована, используйте подготовленный образ
или локальный репозиторий пакетов Debian 13 amd64. Список пакетов из среды
сборки записан в `offline-kit/base-image.txt`. Python-колёса из комплекта
устанавливаются отдельно на шаге 3.

На подключённой машине с подготовляемым образом для полного профиля
установите системные пакеты перед отключением сети.

```bash
sudo apt-get update
sudo apt-get install python3 python3-venv bash coreutils curl iproute2 \
  tar gzip libc-bin libgomp1 libstdc++6 libgcc-s1 libssl3t64 zlib1g libzstd1
```

Если каталог `offline-kit/` уже собран, используйте его целиком. Для новой
сборки на подключённой машине с Debian 13 amd64 установите `python3-pip`
и откройте корень репозитория. Введите идентификатор подготовленного образа
Debian, например ссылку на образ с его digest. Запускайте сборку с той же
минорной версией CPython, которая будет использоваться на цели.

```bash
read -r -p 'Идентификатор базового образа Debian: ' MCP_IMAGE_ID
python3 scripts/build-offline-kit.py offline-kit --stage modules --image-id "$MCP_IMAGE_ID"
```

Команда выше собирает комплект для запуска ядра и демонстрационного модуля.
Для комплекта с моделью выполните вместо неё сборку релиза с архивом llama.cpp
и GGUF, подготовленными по руководству приёмки. Сборщик проверит SHA-256
обоих файлов перед копированием. Используйте свободный путь назначения для
каждой сборки. [Подготовка полного комплекта](quickstart.md#подготовить-комплект)

## 2. Перенесите и проверьте комплект

Скопируйте каталог `offline-kit/` на разрешённый носитель и с него на целевую
машину. В примерах комплект хранится в `$HOME/mcp-offline/offline-kit`,
а окружение, журнал и при необходимости движок — в соседнем каталоге
`mcp-core-runtime`. На цели команды выполняются от пользователя, под
которым будет запущен сервер.

```bash
mkdir -p "$HOME/mcp-offline"
cd "$HOME/mcp-offline"
read -r -p 'Путь к каталогу offline-kit на носителе: ' MCP_SOURCE_KIT
test ! -e offline-kit && cp -a -- "$MCP_SOURCE_KIT" offline-kit
```

В каждой новой оболочке начните с определения путей. Если выбрали другое
место для комплекта или окружения, подставляйте те же пути во всех оболочках.

```bash
MCP_ROOT="$HOME/mcp-offline"
MCP_KIT="$MCP_ROOT/offline-kit"
MCP_RUNTIME="$MCP_ROOT/mcp-core-runtime"
MCP_PYTHON=/usr/bin/python3
```

Проверьте архитектуру, интерпретатор, loopback и контрольные суммы. В выводе
`ip -br addr show dev lo` ожидается `127.0.0.1/8`. Продолжайте после
успешной проверки всех файлов. Если есть `FAILED`, повторите перенос
из исходного комплекта.

```bash
(
  set -eu
  test "$(dpkg --print-architecture)" = amd64
  "$MCP_PYTHON" --version
  "$MCP_PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 13))'
  ip -br addr show dev lo
  cd "$MCP_KIT"
  sha256sum --check SHA256SUMS
)
```

## 3. Установите сервер из wheelhouse

Используйте новый каталог исполнения. Если он уже существует, задайте
другой `MCP_RUNTIME` и используйте его в следующих командах. Активация
виртуальной среды не нужна, команды используют абсолютные пути.

```bash
(
  set -eu
  mkdir -- "$MCP_RUNTIME"
  "$MCP_PYTHON" -m venv "$MCP_RUNTIME/venv"
  "$MCP_RUNTIME/venv/bin/python" -m pip --isolated install \
    --no-cache-dir --no-index --find-links "$MCP_KIT/wheelhouse" \
    --only-binary=:all: --require-hashes -r "$MCP_KIT/requirements.lock"
  "$MCP_RUNTIME/venv/bin/python" -m pip check
  "$MCP_RUNTIME/venv/bin/mcp-core" --version
  "$MCP_RUNTIME/venv/bin/mcp-core-check" --version
)
```

Успешная проверка завершается сообщением `No broken requirements found.`
и версиями обеих команд. Для комплекта первой поставки обе версии равны
`0.1.0`. При ошибке `No matching distribution found` сверьте версию
Python в `base-image.txt` с выбранным интерпретатором и архитектуру wheels
с целью.

## 4. Проверьте ядро без модели

Запустите проверочный клиент. Он последовательно создаст два временных
сервера на портах, выбранных ОС, и завершит их после проверки.

```bash
"$MCP_RUNTIME/venv/bin/mcp-core-check" core \
  --empty-config "$MCP_KIT/examples/empty.toml" \
  --demo-config "$MCP_KIT/examples/demo.toml"
```

При успехе в JSON-выводе будут `"protocol": "2026-07-28"`,
`"empty_tools": []`, `"tool": "demo_echo"` и
`"result": {"echo": "mcp-check-001"}`. Адреса временных серверов
могут меняться от запуска к запуску.

## 5. Запустите сервер и подключите локальный клиент

В первой оболочке запустите сервер в переднем плане. С конфигурацией
`demo.toml` в каталоге будет `demo_echo`. Для пустого каталога
передайте `examples/empty.toml`.

```bash
"$MCP_RUNTIME/venv/bin/mcp-core" --config "$MCP_KIT/examples/demo.toml" --port 8765 \
  2>> "$MCP_RUNTIME/mcp-core.log"
```

Сервер объявит адрес одной строкой `{"mcp_url": "http://127.0.0.1:8765/mcp"}`.
Диагностика старта и записи о вызовах попадут в `mcp-core.log`. Оставьте
эту оболочку открытой. Для остановки нажмите Ctrl+C.

Во второй оболочке задайте переменные путей из шага 2. Укажите в локальном
MCP-клиенте транспорт **Streamable HTTP** и URL `http://127.0.0.1:8765/mcp`.
Для локального соединения используйте прямой доступ к loopback без прокси.
Оставьте HTTP-заголовок `Host`, который клиент формирует из этого URL.
Заголовок `Origin` должен отсутствовать. Эти параметры подходят и для
проверки через установленный Python SDK.

```bash
"$MCP_RUNTIME/venv/bin/python" - <<'PY'
import asyncio
import json

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


async def check():
    url = "http://127.0.0.1:8765/mcp"
    async with asyncio.timeout(15), httpx2.AsyncClient(trust_env=False) as http:
        async with Client(streamable_http_client(url, http_client=http), cache=None) as client:
            tools = [tool.name for tool in (await client.list_tools()).tools]
            if client.protocol_version != "2026-07-28" or "demo_echo" not in tools:
                raise SystemExit("Неожиданная версия MCP или отсутствует demo_echo")
            result = await client.call_tool("demo_echo", {"text": "mcp-check-001"})
            if result.is_error or result.structured_content != {"echo": "mcp-check-001"}:
                raise SystemExit("Неожиданный результат demo_echo")
            print(json.dumps({"tools": tools, "result": result.structured_content}))


asyncio.run(check())
PY
```

На конфигурации `demo.toml` ожидается
`{"tools": ["demo_echo"], "result": {"echo": "mcp-check-001"}}`.
Если клиент сам управляет процессом, передайте ему абсолютный путь к
`mcp-core` и аргументы `--config <абсолютный путь> --port 0`. Он должен
прочитать `mcp_url` из stdout и дождаться успешного MCP discovery. Так
работает проверочный клиент из шага 4. [Контракт запуска и discovery](contracts/acceptance-host.md)

## 6. Используйте собственную конфигурацию

Храните рабочую конфигурацию отдельно от переносимого комплекта, чтобы
`SHA256SUMS` продолжал покрывать исходные файлы. Пример ниже создаёт профиль
на основе демонстрационного. Относительный путь `manifest` считается от
каталога TOML, поэтому оба файла копируются вместе.

```bash
mkdir -p "$MCP_RUNTIME/config"
cp --update=none -- "$MCP_KIT/examples/demo.toml" "$MCP_KIT/examples/demo-module.json" "$MCP_RUNTIME/config/"
```

Для следующего запуска используйте
`--config "$MCP_RUNTIME/config/demo.toml"`. Ядро читает конфигурацию и
манифесты при старте, поэтому после их изменения перезапустите сервер.
Верхнеуровневое поле `disabled_tools` принимает список имён
зарегистрированных инструментов. В `grants` перечисляются полномочия для
локальных клиентов этого экземпляра, а параметры `execution.timeout_seconds`
и `execution.max_concurrent_calls` ограничивают вызовы. По умолчанию
действуют 10 секунд и четыре слота. Пакет доверенного стороннего модуля
и его Python-зависимости нужно установить в то же `venv` из заранее
подготовленных локальных wheels и явно указать JSON-манифест в TOML.
[Конфигурация сервера](contracts/server-mcp.md) · [Контракт модулей](contracts/module-v1.md)

Проверенные примеры отдельных пакетов в `tools/` можно собрать по
[руководству по ZIP-инструменту](add-zip-tool.md) или
[руководству по PDF-инструменту](add-pdf-tool.md).

## 7. Проверьте связку с моделью при необходимости

Для этого шага нужен полный комплект с каталогами `engine/` и `model/`.
Движок llama.cpp и MCP-сервер используют разные HTTP-порты на той же машине.
В отдельной оболочке с путями из шага 2 распакуйте архив, проверьте
библиотеки и версию движка.

```bash
mkdir -p "$MCP_RUNTIME/engine"
tar --no-same-owner -xzf "$MCP_KIT/engine/llama-b12359-bin-ubuntu-x64.tar.gz" \
  -C "$MCP_RUNTIME/engine"
MCP_LLAMA="$MCP_RUNTIME/engine/llama-b12359/llama-server"
ldd "$MCP_LLAMA"
"$MCP_LLAMA" --version
```

Если `ldd` выводит `not found`, подготовьте недостающую системную
библиотеку для Debian 13 и повторите проверку. При успешной проверке
запустите модель в этой оболочке.

```bash
"$MCP_LLAMA" --offline --host 127.0.0.1 --port 9931 \
  --model "$MCP_KIT/model/Qwen3-4B-Q4_K_M.gguf" --alias qwen3-4b \
  --ctx-size 4096 --parallel 1 --n-gpu-layers 0 --jinja --no-ui \
  --chat-template-kwargs '{"enable_thinking":false}'
```

Когда модель загрузится, в другой оболочке с путями из шага 2 проверьте
`/health` и запустите сквозной тест. Хост сам запустит временный
MCP-сервер и использует API модели по адресу
`http://127.0.0.1:9931/v1/chat/completions`.

```bash
curl --noproxy '*' --max-time 5 --fail --silent --show-error http://127.0.0.1:9931/health
"$MCP_RUNTIME/venv/bin/mcp-core-check" e2e --config "$MCP_KIT/examples/demo.toml"
```

JSON-вывод `e2e` покажет выбранный моделью инструмент, аргументы, результат
MCP-вызова, сообщение с исходным ID и финальный ответ модели. Остановить движок
можно Ctrl+C в его оболочке. Для полной приёмки, включая проверку на второй
машине, используйте [руководство приёмки](quickstart.md).

## Диагностика запуска

| Симптом | Что проверить |
|---|---|
| `cannot read configuration` | Проверьте путь после `--config` и доступ пользователя к TOML. |
| `cannot read a valid manifest` | Проверьте путь `manifest`, каталог TOML и корректность JSON. |
| `cannot bind 127.0.0.1:8765` | Проверьте `ss -lntp`. Укажите свободный порт в команде и в URL клиента или освободите порт штатной остановкой своего процесса. |
| HTTP 421 или 403 | Используйте адрес `127.0.0.1` с фактическим портом в URL и проверьте отсутствие `Origin`. Uvicorn также может вернуть 400 при отсутствующем `Host`. |
| `MCP startup or discovery failed` | Запустите `mcp-core` вручную с той же конфигурацией и проверьте его stderr или `mcp-core.log`. |
| `model request failed` | Проверьте локальный `/health`, порт 9931, запуск с псевдонимом `qwen3-4b` и вывод llama.cpp. Работу самого MCP проверьте командой `mcp-core-check core`. |
