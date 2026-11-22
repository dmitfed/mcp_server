# Развёртывание офлайн MCP-сервера с vLLM

MCP-хост, сервер инструментов и vLLM работают на одной машине. Хост
передаёт модели описание инструментов, исполняет выбранный вызов через
MCP и возвращает модели результат. В этом руководстве используется
OpenAI-совместимый API Chat Completions.

```text
MCP-хост ── Streamable HTTP ── mcp-core на 127.0.0.1:<порт ОС>/mcp
    └────── Chat Completions ── vLLM на 127.0.0.1:8000/v1/chat/completions
```

Пример рассчитан на Debian 13 amd64 с CPython 3.13 для MCP и уже
установленным vLLM. Команды запуска сверены для vLLM `0.33.0` и
`Qwen/Qwen3-4B-Instruct-2507` в формате Safetensors. Для MCP создаётся
отдельное окружение. [Параметры `vllm serve`](https://docs.vllm.ai/en/v0.33.0/cli/serve/)

## 1. Проверьте установленный vLLM

Откройте оболочку, в которой обычно запускаете vLLM. Если он установлен
в своём `venv`, активируйте это окружение или используйте абсолютный
путь к его исполняемому файлу. На целевой машине проверьте версию и
доступность параметров запуска.

```bash
command -v vllm
vllm --version
vllm serve --help=all
```

В справке нужны `--enable-auto-tool-choice`, `--tool-call-parser`
и `--served-model-name`. Если синтаксис справки отличается, используйте
`vllm serve --help` и документацию своей версии. Для установки MCP
понадобятся `python3`, `python3-venv`, Bash, `coreutils`, `curl` и
`iproute2`. [Установка MCP](deployment.md#1-подготовьте-debian-и-переносимый-комплект) · [Справка vLLM](https://docs.vllm.ai/en/latest/cli/)

У BF16-весов модели из примера около 8 ГБ. Дополнительно нужны память
для KV-кэша и процесса vLLM. Для GPU проверьте свободную видеопамять,
например через `nvidia-smi` на NVIDIA. Для CPU проверьте `free -h`.
[Профиль модели Qwen](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507) · [Настройки vLLM на CPU](https://docs.vllm.ai/en/v0.33.0/getting_started/installation/cpu/)

## 2. Соберите переносимый комплект на подключённой машине

Для сборки wheelhouse используйте Debian 13 amd64 с CPython 3.13
и `python3-pip`. Выполните из корня репозитория. Укажите метку базовой
системы для `mcp-kit/base-image.txt`, например `debian-13-amd64-v1`. Каталог
`$HOME/mcp-vllm-bundle` должен быть свободен.

```bash
(
  set -euo pipefail
  MCP_BUNDLE="$HOME/mcp-vllm-bundle"
  read -r -p 'Идентификатор базовой системы Debian: ' MCP_IMAGE_ID

  mkdir -- "$MCP_BUNDLE"
  python3 scripts/build-offline-kit.py "$MCP_BUNDLE/mcp-kit" \
    --stage modules --image-id "$MCP_IMAGE_ID"
)
```

Если модель уже есть на целевой машине, используйте её локальный
каталог в шаге 4. Если файлов ещё нет, на подключённой машине скачайте
конкретную ревизию Qwen вместе с конфигурацией, токенизатором, шаблоном
диалога и всеми частями весов. [Команда `hf download`](https://huggingface.co/docs/huggingface_hub/en/guides/cli#download-files) · [Зафиксированная ревизия Qwen](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507/tree/cdbee75f17c01a7cc42f958dc650907174af0554)

```bash
(
  set -euo pipefail
  MCP_BUNDLE="$HOME/mcp-vllm-bundle"
  MCP_MODEL=Qwen/Qwen3-4B-Instruct-2507
  MCP_MODEL_REV=cdbee75f17c01a7cc42f958dc650907174af0554
  hf download "$MCP_MODEL" --revision "$MCP_MODEL_REV" --local-dir "$MCP_BUNDLE/model"
  printf '%s %s\n' "$MCP_MODEL" "$MCP_MODEL_REV" > "$MCP_BUNDLE/model-revision.txt"

  cd "$MCP_BUNDLE"
  {
    printf '%s\0' model-revision.txt
    find model -path 'model/.cache' -prune -o -type f -print0
  } | sort -z | xargs -0 sha256sum -- > MODEL.SHA256SUMS
)
```

Перенесите `mcp-vllm-bundle/` на цель. Комплект MCP содержит свою
ведомость SHA-256. При переносе модели вместе с комплектом проверьте
также `MODEL.SHA256SUMS`.

## 3. Проверьте комплект и установите MCP без интернета

Положите перенесённый каталог в `$HOME/mcp-vllm-bundle`. Выполните
установку от пользователя, который запускает MCP. В примере создаётся
новый `$HOME/mcp-vllm-runtime`.

```bash
(
  set -euo pipefail
  MCP_BUNDLE="$HOME/mcp-vllm-bundle"
  MCP_KIT="$MCP_BUNDLE/mcp-kit"
  MCP_RUNTIME="$HOME/mcp-vllm-runtime"
  test "$(dpkg --print-architecture)" = amd64
  python3 --version
  ip -br addr show dev lo
  if [ -e "$MCP_BUNDLE/MODEL.SHA256SUMS" ]; then
    (cd "$MCP_BUNDLE" && sha256sum --check MODEL.SHA256SUMS)
  fi
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
)
```

В конце ожидаются пустой каталог для `empty.toml` и
`{"echo":"mcp-check-001"}` для `demo.toml`. Для этой проверки
состояние процесса vLLM не имеет значения.

## 4. Запустите vLLM на loopback

В первой оболочке запустите установленный `vllm`. Если модель
хранится в другом месте, заранее задайте её абсолютный путь через
`export MCP_MODEL_DIR=/путь/к/модели`. Если используете абсолютный путь
к исполняемому файлу, задайте его через `MCP_VLLM_BIN`.
Параметр `--host 127.0.0.1` оставляет API на loopback. Переменная
`HF_HUB_OFFLINE=1` включает офлайн-режим для Hugging Face Hub.
[Офлайн-режим Hugging Face](https://huggingface.co/docs/huggingface_hub/en/package_reference/environment_variables#hfhuboffline) · [Настройки телеметрии vLLM](https://docs.vllm.ai/en/v0.33.0/configuration/env_vars/)

```bash
(
  set -euo pipefail
  MCP_MODEL_DIR="${MCP_MODEL_DIR:-$HOME/mcp-vllm-bundle/model}"
  MCP_VLLM_BIN="${MCP_VLLM_BIN:-$(command -v vllm)}"
  test -s "$MCP_MODEL_DIR/config.json"

  HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 VLLM_NO_USAGE_STATS=1 \
    "$MCP_VLLM_BIN" serve "$MCP_MODEL_DIR" \
    --served-model-name qwen3-4b-instruct \
    --host 127.0.0.1 --port 8000 --dtype bfloat16 \
    --max-model-len 4096 --max-num-seqs 1 \
    --enable-auto-tool-choice --tool-call-parser hermes
)
```

Шаблон диалога поставляется в `tokenizer_config.json` этой ревизии Qwen
и использует Hermes-формат вызовов. Флаги запуска включают его обработку
в API vLLM. Ограничения контекста и числа одновременных запросов выбраны
для одной проверочной беседы. Для другой модели возьмите соответствующий
ей парсер. При запуске на CPU можно задать
`export VLLM_CPU_KVCACHE_SPACE=2` перед командой. Это выделит 2 ГиБ для
KV-кэша. [Вызов инструментов Qwen](https://qwen.readthedocs.io/en/latest/framework/function_call.html) · [Настройки вызова инструментов vLLM](https://docs.vllm.ai/en/v0.33.0/features/tool_calling/) · [Настройки CPU](https://docs.vllm.ai/en/v0.33.0/getting_started/installation/cpu/)

Дождитесь загрузки весов. Во второй оболочке проверьте здоровье сервера,
псевдоним модели и адрес слушающего сокета.

```bash
curl --noproxy '*' -fsS --max-time 5 http://127.0.0.1:8000/health
curl --noproxy '*' -fsS --max-time 5 http://127.0.0.1:8000/v1/models
ss -lnt 'sport = :8000'
```

Ожидаются HTTP 200 на `/health`, модель с `id` `qwen3-4b-instruct`
и слушающий сокет `127.0.0.1:8000`. [API vLLM](https://docs.vllm.ai/en/stable/serving/openai_compatible_server/)

Если vLLM уже запущен с поддержкой вызовов инструментов на loopback,
используйте его. Подставьте фактический порт и `id` из `/v1/models`
в проверочные команды и код хоста из шага 5.

## 5. Выполните сквозной вызов через MCP-хост

Запустите код во второй оболочке. Для запуска дочернего сервера пример
переиспользует внутренний помощник `_mcp` из установленной версии ядра
`0.1.0`. Он запускает `mcp-core --port 0`, читает `mcp_url`,
подтверждает готовность через MCP discovery и останавливает дочерний
процесс по завершении. Если пишете свой хост, используйте тот же контракт
запуска и SDK-клиент Streamable HTTP. [Контракт проверочного хоста](contracts/acceptance-host.md)

Хост получает схему `demo_echo` через `tools/list`. Первый запрос
выбирает инструмент по имени для воспроизводимой проверки. Затем хост
выполняет вызов и передаёт фактический результат как сообщение `role: tool`
с исходным ID. Второй запрос отключает новые вызовы, чтобы модель ответила
текстом. [Выбор инструмента в vLLM](https://docs.vllm.ai/en/v0.33.0/features/tool_calling/#named-function-calling)

```bash
MCP_RUNTIME="$HOME/mcp-vllm-runtime"
MCP_KIT="$HOME/mcp-vllm-bundle/mcp-kit"
"$MCP_RUNTIME/venv/bin/python" - "$MCP_KIT/examples/demo.toml" <<'PY'
import asyncio
import json
from pathlib import Path
import sys

import httpx2

from mcp_offline_core.acceptance import _mcp


async def chat(http, tools, messages, choice):
    async with asyncio.timeout(120):
        reply = await http.post(
            "http://127.0.0.1:8000/v1/chat/completions",
            json={
                "model": "qwen3-4b-instruct",
                "messages": messages,
                "tools": tools,
                "tool_choice": choice,
                "parallel_tool_calls": False,
                "temperature": 0,
                "max_tokens": 128,
            },
        )
        reply.raise_for_status()
        return reply.json()["choices"][0]["message"]


async def main(config):
    async with (
        _mcp(config) as (client, address),
        httpx2.AsyncClient(trust_env=False, follow_redirects=False, timeout=120) as http,
    ):
        async with asyncio.timeout(15):
            listing = await client.list_tools()
        tool = next((item for item in listing.tools if item.name == "demo_echo"), None)
        if tool is None:
            raise RuntimeError("В каталоге MCP нет demo_echo")
        tools = [{
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }]
        user = {
            "role": "user",
            "content": 'Call demo_echo exactly once with text "mcp-vllm-001", then report its result.',
        }
        choice = {"type": "function", "function": {"name": tool.name}}
        assistant = await chat(http, tools, [user], choice)
        calls = assistant.get("tool_calls") or []
        if len(calls) != 1:
            raise RuntimeError("vLLM должен вернуть один вызов инструмента")
        call = calls[0]
        function = call.get("function") or {}
        call_id = call.get("id")
        if call.get("type") != "function" or function.get("name") != tool.name or not call_id:
            raise RuntimeError("vLLM вернул неожиданный инструмент или ID")
        arguments = json.loads(function["arguments"])
        if arguments != {"text": "mcp-vllm-001"}:
            raise RuntimeError("vLLM вернул неожиданные аргументы")

        async with asyncio.timeout(15):
            result = await client.call_tool(tool.name, arguments)
        if result.is_error or result.structured_content != {"echo": "mcp-vllm-001"}:
            raise RuntimeError("MCP вернул неожиданный результат")
        history = [
            user,
            {"role": "assistant", "content": assistant.get("content"), "tool_calls": calls},
            {"role": "tool", "tool_call_id": call_id,
             "content": json.dumps(result.structured_content, ensure_ascii=False)},
        ]
        final = (await chat(http, tools, history, "none")).get("content")
        if not isinstance(final, str) or not final.strip():
            raise RuntimeError("vLLM вернул пустой итоговый ответ")
        print(json.dumps({
            "mcp_url": address,
            "tool": tool.name,
            "tool_call_id": call_id,
            "result": result.structured_content,
            "final_answer": final,
        }, ensure_ascii=False, indent=2))


asyncio.run(main(Path(sys.argv[1])))
PY
```

Успешный вывод содержит `demo_echo`, ID вызова, результат
`{"echo":"mcp-vllm-001"}` и непустой `final_answer`. Для обычного
автоматического выбора инструментов хост может передавать
`tool_choice: "auto"`. В таком режиме он обрабатывает и обычный ответ
модели без вызова. Для подключения ZIP или PDF сначала установите их
wheels с зависимостями в окружение MCP и зарегистрируйте манифесты. Хост
должен передавать модели только инструменты, разрешённые для сценария.
[ZIP](add-zip-tool.md) · [PDF](add-pdf-tool.md)

## 6. Диагностика и остановка

| Симптом | Что проверить |
|---|---|
| Команда `vllm` не найдена или параметр не поддерживается | Используйте окружение установленного vLLM и сверяйте флаги с его версией. |
| vLLM сообщает об отсутствующих файлах или попытке обращения к Hub | Укажите абсолютный путь к модели. Проверьте конфигурацию, токенизатор, индекс и все части Safetensors. Для перенесённой модели сверьте `MODEL.SHA256SUMS`. |
| GPU недоступна | Для NVIDIA проверьте `nvidia-smi` и журнал запуска установленного vLLM. |
| vLLM не хватает памяти | Уменьшите `--max-model-len`. Для CPU проверьте свободную RAM и размер `VLLM_CPU_KVCACHE_SPACE`. Для GPU проверьте свободную VRAM. Веса должны помещаться даже при коротком контексте. |
| HTTP 404 или неизвестная модель | Сверьте `/v1/chat/completions` и значение `model` с `id` из `/v1/models`. |
| vLLM не возвращает `tool_calls` | Сверьте версию vLLM, ревизию модели, `tokenizer_config.json`, `--enable-auto-tool-choice` и `--tool-call-parser hermes`. Первый запрос из примера задаёт инструмент по имени. |
| Хост не может запустить MCP или найти `demo_echo` | Проверьте MCP командой ниже. Для ручной диагностики запустите `"$HOME/mcp-vllm-runtime/venv/bin/mcp-core" --config "$HOME/mcp-vllm-bundle/mcp-kit/examples/demo.toml" --port 8765` и посмотрите stderr. |

Если запускали vLLM в первой оболочке, остановите его там через Ctrl+C.
Затем можно повторить проверку MCP при остановленной модели.

```bash
MCP_RUNTIME="$HOME/mcp-vllm-runtime"
MCP_KIT="$HOME/mcp-vllm-bundle/mcp-kit"
"$MCP_RUNTIME/venv/bin/mcp-core-check" core \
  --empty-config "$MCP_KIT/examples/empty.toml" \
  --demo-config "$MCP_KIT/examples/demo.toml"
```
