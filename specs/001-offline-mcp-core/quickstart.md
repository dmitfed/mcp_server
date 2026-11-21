# Руководство приёмки — офлайн-ядро MCP-сервера

[План](plan.md) · [Версии и хеши источников](research.md) ·
[Контракт проверочной CLI](contracts/acceptance-host.md)

Для установки и штатного запуска используйте [пошаговое руководство по развёртыванию](deployment.md).

Команды системы, движка и установки используют выбранный профиль. Команды
`mcp-core` и `mcp-core-check` предназначены для приёмки собранного релиза.

## Подготовить комплект

### Базовый образ

Подготовить Debian 13 x86-64 (amd64) с Python ≥ 3.13. Для первого прогона
использовать системные пакеты `python3` и `python3-venv`. В образе нужны
`bash`, `tar`, `gzip`, `coreutils` для `sha256sum`, `curl`, `libc-bin` для
`ldd` и `iproute2` для `ss`. На второй машине для сетевой проверки нужны
`curl` и `iputils-ping`.

До офлайн-приёмки зафиксировать идентификатор образа и перечень системных
пакетов с точными версиями. В перечень включить пакеты библиотек, необходимых
архиву движка на этом образе. При подготовке образа их определяют по проверке
`ldd` и загрузке движка на Debian 13. Сохранить перечень вместе с описанием
профиля приёмки. [Пакет libc-bin](https://packages.debian.org/trixie/libc-bin) · [Пакет iputils-ping](https://packages.debian.org/trixie/iputils-ping)

### Переносимый комплект

Сборка на Debian 13 amd64 выпускает wheel проекта с обоими CLI, скачивает
wheels всех зависимостей для целевого интерпретатора и формирует
`requirements.lock` с точными версиями и SHA-256, включая wheel проекта.
При использовании более новой версии Python подготовить комплект для неё.
Финальный комплект собирается в новом `offline-kit/`. В его `wheelhouse`
должен быть один wheel проекта с готовой командой `e2e`. Для него и всех
зависимостей заново формируется `requirements.lock`, затем считается
`SHA256SUMS` по фактическим файлам комплекта. Структура комплекта приведена ниже.

```text
offline-kit/
├── SHA256SUMS
├── requirements.lock
├── base-image.txt               # идентификатор образа и точные версии пакетов
├── wheelhouse/
├── examples/                    # empty.toml, demo.toml, demo-module.json
├── tests/                       # офлайн-проверки ядра и их фикстуры
├── engine/
│   └── llama-b12359-bin-ubuntu-x64.tar.gz
└── model/
    └── Qwen3-4B-Q4_K_M.gguf
```

На подключённой машине подготовить внешние артефакты отдельно от финального
комплекта:

```bash
INPUTS="$PWD/offline-kit-inputs"
mkdir -p "$INPUTS/engine" "$INPUTS/model"
curl -fL 'https://github.com/ggml-org/llama.cpp/releases/download/b12359/llama-b12359-bin-ubuntu-x64.tar.gz' \
  -o "$INPUTS/engine/llama-b12359-bin-ubuntu-x64.tar.gz"
hf download Qwen/Qwen3-4B-GGUF Qwen3-4B-Q4_K_M.gguf \
  --revision a9a60d009fa7ff9606305047c2bf77ac25dbec49 --local-dir "$INPUTS/model"
```

На подключённой машине с Debian 13 amd64 создать комплект из корня репозитория.
Указать фактический идентификатор подготовленного образа в `IMAGE_ID`.

```bash
python3 scripts/build-offline-kit.py offline-kit --image-id "$IMAGE_ID" --inputs "$INPUTS"
```

Скрипт сверяет SHA-256 архива и GGUF со значениями в исследовании, собирает
wheelhouse и формирует ведомость `SHA256SUMS`. Перенести весь каталог `offline-kit/`.
Для целевой машины требуются сам GGUF и описанные артефакты. Команда `hf`
выполняется только на машине подготовки. Имя CPU-архива содержит `ubuntu`
по имени upstream-сборки. Проверка загрузки бинарника выполняется на Debian 13.

## Установить и проверить ядро без модели

Начать прогон на зафиксированном базовом образе без маршрута в интернет.
Использовать только задокументированные системные предпосылки и переносимый
комплект. Для каждого прогона создать новый каталог исполнения и новый `venv`.
Предустановки проекта, предыдущие окружения и внешние кэши не входят в
предпосылки. Если путь каталога уже занят, выбрать другой путь и использовать
его во всех командах текущего прогона. Сохранить loopback. Для SC-005 и SC-006
подготовить вторую машину в изолированной сети с маршрутом к целевой машине.

```bash
KIT="$PWD/offline-kit"
RUNTIME="$PWD/mcp-core-runtime"
PYTHON_BIN="${MCP_CORE_PYTHON:-python3}"
(
  set -e
  (cd "$KIT" && sha256sum --check SHA256SUMS)
  "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 13))'
  mkdir -- "$RUNTIME"
  "$PYTHON_BIN" -m venv "$RUNTIME/venv"
  "$RUNTIME/venv/bin/python" -m pip --isolated install \
    --no-cache-dir --no-index --find-links "$KIT/wheelhouse" \
    --only-binary=:all: --require-hashes \
    -r "$KIT/requirements.lock"
  "$RUNTIME/venv/bin/python" -m pip check
  "$RUNTIME/venv/bin/mcp-core" --version
  "$RUNTIME/venv/bin/mcp-core-check" --version
  "$RUNTIME/venv/bin/mcp-core-check" core \
    --empty-config "$KIT/examples/empty.toml" --demo-config "$KIT/examples/demo.toml"
  (cd "$KIT" && "$RUNTIME/venv/bin/python" -m unittest discover -s tests -v)
)
```

Ожидаемые результаты:

| Проверка | Наблюдение |
|---|---|
| SC-001 и SC-002 | Версия хоста `0.1.0`. Клиент получает пустой каталог, затем в отдельном запуске видит `demo_echo` и получает ожидаемый объект. Оба запуска проходят при остановленном движке. |
| FR-001 и FR-003 | Отсутствующий конфиг, отсутствующий обязательный манифест или конфликт имени видны в диагностике запуска. Модуль из тестового пакета подключается штатной конфигурацией. |
| FR-005 и FR-006 | Ошибочные аргументы, неизвестное или отключённое имя и неверные параметры MCP отклоняются. Фикстура защищённой операции проверяет отказ без требуемого полномочия. |
| SC-003 | Ошибка обработчика и таймаут возвращают ошибки инструмента. Ответ таймаута приходит до нормального завершения искусственно задержанной операции. После каждого сбоя доступны каталог и второй инструмент. Журналы содержат ожидаемые записи и ноль контрольных значений. |
| Запуск хостом | Хост запускает дочерний процесс на выделенном ОС loopback-порту, обнаруживает MCP endpoint через HTTP и собирает дочерний процесс по завершении проверки. |

## Проверить модель и сквозной диалог

Распаковать каталог движка целиком. Перед приёмкой проверить загрузку библиотеки
и версию бинарника. Вывод `ldd` не должен содержать строк `not found`.

```bash
mkdir -p "$RUNTIME/engine"
tar --no-same-owner -xzf "$KIT/engine/llama-b12359-bin-ubuntu-x64.tar.gz" -C "$RUNTIME/engine"
LLAMA_SERVER="$RUNTIME/engine/llama-b12359/llama-server"
ldd "$LLAMA_SERVER"
"$LLAMA_SERVER" --version
"$LLAMA_SERVER" --offline --host 127.0.0.1 --port 9931 \
  --model "$KIT/model/Qwen3-4B-Q4_K_M.gguf" --alias qwen3-4b \
  --ctx-size 4096 --parallel 1 --n-gpu-layers 0 --jinja --no-ui \
  --chat-template-kwargs '{"enable_thinking":false}'
```

После готовности движка выполнить в отдельной оболочке из того же каталога:

```bash
KIT="$PWD/offline-kit"
RUNTIME="$PWD/mcp-core-runtime"
curl --noproxy '*' --max-time 5 -fsS http://127.0.0.1:9931/health
"$RUNTIME/venv/bin/mcp-core-check" e2e --config "$KIT/examples/demo.toml"
```

Для SC-004 вывод проверки подтверждает обнаружение `demo_echo`, ожидаемые
аргументы и результат, передачу результата с исходным ID вызова и непустой
финальный ответ. Оставить движок запущенным для проверки сетевой границы.

## Проверить границу локального доступа

Для проверки SC-006 запустить отдельный экземпляр MCP-сервера на фиксированном
порту. Сервер объявляет `http://127.0.0.1:8765/mcp`:

```bash
KIT="$PWD/offline-kit"
RUNTIME="$PWD/mcp-core-runtime"
"$RUNTIME/venv/bin/mcp-core" --config "$KIT/examples/demo.toml" --port 8765
```

На целевой машине подтвердить через `ss -lntp` привязку MCP только к
`127.0.0.1:8765`, а движка только к `127.0.0.1:9931`. Записать PID
MCP-экземпляра для проверки после остановки модели. Проверить правила для
`Host` и `Origin` локальными запросами к запущенному экземпляру:

```bash
probe_mcp() {
  curl --http1.1 --noproxy '*' --max-time 5 --silent --show-error --output /dev/null \
    --write-out '%{http_code}\n' \
    --header 'Accept: application/json, text/event-stream' \
    --header 'Content-Type: application/json' \
    "$@" --data '{"jsonrpc":"2.0","method":"server/discover","id":1}' \
    http://127.0.0.1:8765/mcp
}
probe_mcp --header 'Host: outside.example'
probe_mcp --header 'Host:'
probe_mcp --header 'Origin: http://outside.example'
probe_mcp --header 'Origin;'
```

В том же порядке ожидаются HTTP 421 для неверного `Host`, HTTP 400 или 421 для
отсутствующего `Host`, HTTP 403 для чужого `Origin` и HTTP 403 для пустого
`Origin`. Перед удалёнными пробами повторить локальный запрос к
`http://127.0.0.1:9931/health` и подтвердить, что модель доступна. Для SC-005
и SC-006 на второй машине проверить доступность целевой машины в изолированной
сети и обратиться к обоим портам по её адресу:

```bash
read -r -p 'IPv4 целевой машины: ' target_ip
ping -c 1 "$target_ip"
curl --noproxy '*' --connect-timeout 3 --max-time 5 \
  --silent --show-error --output /dev/null "http://$target_ip:9931/health"
curl --noproxy '*' --connect-timeout 3 --max-time 5 \
  --silent --show-error --output /dev/null "http://$target_ip:8765/mcp"
```

При подтверждённом сетевом маршруте оба запроса завершаются ошибками соединения
без HTTP-ответов. Отказ соединения с MCP-портом исключает как получение каталога,
так и вызов инструмента. Локальный запрос к модели из предыдущего шага успешен.

## Проверить MCP после остановки модели

После сетевых проб, пока модель работает, выполнить проверку каталога и вызова
на уже запущенном MCP-экземпляре с фиксированным портом:

```bash
RUNTIME="$PWD/mcp-core-runtime"
NO_PROXY=127.0.0.1 no_proxy=127.0.0.1 "$RUNTIME/venv/bin/python" - <<'PY'
import asyncio

from mcp import Client


async def check_mcp():
    async with asyncio.timeout(15), Client("http://127.0.0.1:8765/mcp") as client:
        listing = await client.list_tools()
        if not any(tool.name == "demo_echo" for tool in listing.tools):
            raise SystemExit("demo_echo is absent from tools/list")
        result = await client.call_tool("demo_echo", {"text": "mcp-check-001"})
        payload = result.model_dump(by_alias=True)
        if payload.get("isError") or payload.get("structuredContent") != {"echo": "mcp-check-001"}:
            raise SystemExit("demo_echo returned an unexpected result")
        print("tools/list and demo_echo succeeded")


asyncio.run(check_mcp())
PY
```

Остановить только движок модели. Запустить `mcp-core-check e2e --config
"$KIT/examples/demo.toml"` тем же способом, что в положительном прогоне.
Ожидается ненулевой код с диагностикой сбоя при обращении к модели. CLI управляет
собственным дочерним процессом и оставляет фиксированный экземпляр MCP запущенным.
Сверить PID через `ss -lntp` и повторить блок с SDK-клиентом. Ожидаются тот же PID,
успешное получение каталога и тот же результат `demo_echo`.

## Зафиксировать результаты приёмки

Итоговый `mcp-core-runtime/acceptance-report.md` относится к одному прогону
финального `offline-kit/`. В отчёте указать фактическую
[конфигурацию интеграции](data-model.md) для этого прогона.

- Идентификатор базового образа, ссылку на перечень системных пакетов с версиями
  и фактические версии ОС, Python, движка, хоста и MCP.
- Имя и ревизию модели, SHA-256 файла GGUF, хеш ведомости `SHA256SUMS` из комплекта
  и хеши фактически использованных файлов конфигурации и манифестов модулей.
- Эффективные адреса, порты и пути обоих соединений, способ выбора MCP-порта
  и флаги запуска движка. Для проверки сетевой границы указать отдельно
  фиксированный MCP-порт.
- Результаты SC-001–SC-006 и наблюдения после остановки модели на том же MCP-экземпляре.
