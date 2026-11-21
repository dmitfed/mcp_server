# Офлайн-сервер MCP

Локальный Python-сервер инструментов для MCP-хостов. Сервер работает без
интернета, принимает запросы по Streamable HTTP на `127.0.0.1` и подключает
инструменты из явно указанных JSON-манифестов. Ядро проверяет аргументы
и запускает обработчик в отдельном процессе с таймаутом.

MCP-хост обращается к инструментам через этот сервер, а к модели через её
локальный HTTP API. Сервер инструментов можно запустить и проверить при
остановленной модели.

```text
MCP-хост ── Streamable HTTP ── MCP-сервер ── обработчики инструментов
    └────── локальный HTTP API ── движок модели
```

**Проверенная конфигурация** — Debian 13 amd64, CPython 3.13, MCP
`2026-07-28`. Сквозная проверка с моделью пройдена на CPU с llama.cpp
b12359 и `Qwen3-4B-Q4_K_M.gguf`.
[Профиль и версии артефактов](specs/001-offline-mcp-core/research.md#первая-конфигурация-с-моделью)

## Быстрый запуск без модели

На подключённой машине с Debian 13 amd64, CPython 3.13 и `python3-pip`
выполните из корня репозитория. Введите идентификатор подготовленного
базового образа Debian. Сборщик создаст новый каталог `offline-kit/` с
wheelhouse, точными версиями и контрольными суммами.
[Подготовка базового образа](specs/001-offline-mcp-core/deployment.md#1-подготовьте-debian-и-переносимый-комплект)

```bash
read -r -p 'Идентификатор базового образа Debian: ' MCP_IMAGE_ID
python3 scripts/build-offline-kit.py offline-kit --stage modules --image-id "$MCP_IMAGE_ID"
```

Перенесите весь `offline-kit/` на целевую машину. В каталоге, где он лежит,
выполните команды ниже. Если имя `mcp-core-runtime` уже занято, укажите
свободный путь в `MCP_RUNTIME`. Проверочная команда `core` проверит пустой
каталог и вызов `demo_echo`, затем сервер запустится в переднем плане.

```bash
(
  set -eu
  MCP_KIT="$PWD/offline-kit"
  MCP_RUNTIME="$PWD/mcp-core-runtime"
  (cd "$MCP_KIT" && sha256sum --check SHA256SUMS)
  mkdir -- "$MCP_RUNTIME"
  python3 -m venv "$MCP_RUNTIME/venv"
  "$MCP_RUNTIME/venv/bin/python" -m pip --isolated install \
    --no-cache-dir --no-index --find-links "$MCP_KIT/wheelhouse" \
    --only-binary=:all: --require-hashes -r "$MCP_KIT/requirements.lock"
  "$MCP_RUNTIME/venv/bin/python" -m pip check
  "$MCP_RUNTIME/venv/bin/mcp-core-check" core \
    --empty-config "$MCP_KIT/examples/empty.toml" \
    --demo-config "$MCP_KIT/examples/demo.toml"
  "$MCP_RUNTIME/venv/bin/mcp-core" --config "$MCP_KIT/examples/demo.toml" --port 8765
)
```

Подключите локальный MCP-клиент по **Streamable HTTP** к
`http://127.0.0.1:8765/mcp`. Конфигурация `demo.toml` публикует
`demo_echo`. Для пустого каталога используйте `empty.toml`.
[Полная инструкция с примером вызова](specs/001-offline-mcp-core/deployment.md)

## Инструменты

Инструменты поставляются как самостоятельные Python-пакеты с собственными
wheels, манифестами и тестами. Распаковка ZIP и чтение PDF реализованы
в репозитории как два примера таких инструментов.

| Инструмент | Назначение | Пакет и установка |
|---|---|---|
| `zip_extract` | Распаковывает локальный ZIP и возвращает список файлов | [`tools/zip/`](tools/zip) · [Руководство](specs/001-offline-mcp-core/add-zip-tool.md) |
| `pdf_read` | Извлекает текст из выбранных страниц PDF через `pypdf` | [`tools/pdf/`](tools/pdf) · [Руководство](specs/001-offline-mcp-core/add-pdf-tool.md) |

Установите выбранные пакеты в окружение `mcp-core` и добавьте пути к их
манифестам в TOML.

У каждого инструмента есть имя, описание, входная JSON Schema и ссылка
на Python-функцию. Полномочия задаются в конфигурации экземпляра.
[Контракт для нового модуля](specs/001-offline-mcp-core/contracts/module-v1.md)

## Документация и исходники

- [Приёмка с llama.cpp и Qwen3](specs/001-offline-mcp-core/quickstart.md#проверить-модель-и-сквозной-диалог) и [контракт проверочного хоста](specs/001-offline-mcp-core/contracts/acceptance-host.md).
- [Конфигурация сервера и HTTP-эндпоинт](specs/001-offline-mcp-core/contracts/server-mcp.md).
- [Код ядра](src/mcp_offline_core), [конфигурации](examples), [тесты ядра](tests) и [сборщик офлайн-комплекта](scripts/build-offline-kit.py).
