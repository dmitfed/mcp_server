# Приёмка офлайн RAG на Debian 13

Команды описывают целевой комплект после реализации пакета. Используйте
Debian 13 amd64 с CPython 3.13. Для базового прогона нужны CPU-артефакты,
локальный диск, 32 ГиБ RAM и процессор с AVX2. Запишите фактическую модель
процессора и число доступных ядер. CUDA 13.2 применяется в отдельном
прогоне ускорения.

[План](plan.md) · [Закреплённые артефакты](research.md) ·
[Контракт инструментов](contracts/rag-tools.md)

## 1. Подготовьте комплект на подключённой машине

Соберите wheel модуля, ядра и зависимостей под CPython 3.13. wheelhouse и
`requirements.lock` с хешами собираются по схеме
[комплекта ядра](../001-offline-mcp-core/deployment.md). Docling и Paddle
используют CPU wheels. В системный комплект входят `libreoffice-writer`,
`libreoffice-math`, `python3-uno` той же сборки LibreOffice, `bubblewrap`,
`tesseract-ocr`, `tesseract-ocr-rus`, `tesseract-ocr-eng` и их системные
зависимости. Зафиксируйте версии пакетов. Скопируйте локальные модели Docling
и Paddle, traineddata и собранные колёса грамматик, включая QML и Kotlin.

Модели GGUF загружаются с полным commit. Пример для эмбеддингов:

```bash
hf download Qwen/Qwen3-Embedding-0.6B-GGUF Qwen3-Embedding-0.6B-Q8_0.gguf \
  --revision 370f27d7550e0def9b39c1f16d3fbaa13aa67728 --local-dir ./rag-offline-kit/models
```

В комплект входят `wheelhouse/`, lock-файл, манифест
`rag-module.json`, `artifacts.lock.json`, `models/`,
`bin/qdrant`, `llama/cpu/bin/llama-server` с библиотеками и
`fixtures/` корпуса с эталонами. Обязательны контрольные суммы и
сведения о системных пакетах. В ведомость входят параметры запуска ролей,
контрольные запросы и документы для эмбеддингов, эталонные векторы и допуски.
В `fixtures/` входит отдельная доверенная тестовая ведомость для `pooling=mean`
с эталонами, заранее полученными на проверенной конфигурации с тем же GGUF
и той же размерностью. Для CUDA
дополнительно переносится
`llama/cuda132/` с бинарником, необходимыми библиотеками и описанием
совместимости драйвера. Комплект проверяется на чистом стенде
перед объявлением поддержки.

## 2. Установите на изолированном стенде

Отключите внешнюю сеть, сохранив loopback. Подготовьте системные пакеты из
перенесённых артефактов. Для первого прогона используйте профиль `cpu`.
Раздел 6 описывает отдельный запуск `cuda132`. Из каталога, где расположен
комплект:

```bash
export RAG_BUNDLE="$PWD/rag-offline-kit"
export RAG_TARGET="${RAG_TARGET:-cpu}"
case "$RAG_TARGET" in
  cpu)
    RAG_ROOT="$PWD/rag-runtime-cpu"
    RAG_LLM_PORT_BASE=8090
    RAG_QDRANT_PORT=6333
    RAG_MCP_PORT=8766
    export CUDA_VISIBLE_DEVICES=""
    ;;
  cuda132)
    : "${RAG_GPU_UUID:?Укажите UUID целевой видеокарты}"
    : "${RAG_GPU_LAYERS:?Укажите положительное число слоёв на GPU}"
    RAG_ROOT="$PWD/rag-runtime-cuda132"
    RAG_LLM_PORT_BASE=8190
    RAG_QDRANT_PORT=6433
    RAG_MCP_PORT=8776
    export CUDA_VISIBLE_DEVICES="$RAG_GPU_UUID"
    ;;
  *)
    printf 'Неизвестный профиль: %s\n' "$RAG_TARGET" >&2
    exit 2
    ;;
esac
export RAG_ROOT RAG_LLM_PORT_BASE RAG_QDRANT_PORT RAG_MCP_PORT
export RAG_LAUNCH_DIR="$RAG_ROOT/state/model-runtime"
export RAG_GENERATOR_URL="http://127.0.0.1:$((RAG_LLM_PORT_BASE + 4))"
export HF_HUB_OFFLINE=1
umask 077
(cd "$RAG_BUNDLE" && sha256sum --check SHA256SUMS) || exit 1
mkdir -p "$RAG_ROOT/sources" "$RAG_ROOT/state" "$RAG_ROOT/log"
python3.13 -m venv "$RAG_ROOT/venv"
"$RAG_ROOT/venv/bin/python" -m pip --isolated install \
  --no-index --find-links "$RAG_BUNDLE/wheelhouse" \
  --only-binary=:all: --require-hashes -r "$RAG_BUNDLE/requirements.lock"
"$RAG_ROOT/venv/bin/python" -m pip check
```

Подготовленные модели и грамматики открываются из локального комплекта при
первом вызове. Все сетевые службы приёмки используют loopback.

## 3. Запустите Qdrant

```bash
cat > "$RAG_ROOT/qdrant.yaml" <<EOF_QDRANT
storage:
  storage_path: "$RAG_ROOT/state/qdrant"
service:
  host: 127.0.0.1
  http_port: $RAG_QDRANT_PORT
  grpc_port: null
  enable_cors: false
telemetry_disabled: true
EOF_QDRANT

if [ ! -f "$RAG_ROOT/state/qdrant.key" ]; then
  python3.13 -c 'import secrets; print(secrets.token_hex(32))' > "$RAG_ROOT/state/qdrant.key"
fi
QDRANT__SERVICE__API_KEY="$(cat "$RAG_ROOT/state/qdrant.key")" \
  "$RAG_BUNDLE/bin/qdrant" --config-path "$RAG_ROOT/qdrant.yaml" \
  > "$RAG_ROOT/log/qdrant.log" 2>&1 &
```

Сверьте готовность Qdrant по `GET /healthz` на порту
`RAG_QDRANT_PORT`.

## 4. Запустите модели и MCP-модуль

```bash
export MCP_RAG_CONFIG="$RAG_ROOT/rag.toml"
cat > "$MCP_RAG_CONFIG" <<EOF_RAG
config_version = 1
source_root = "$RAG_ROOT/sources"
state_dir = "$RAG_ROOT/state"
diagnostics_path = "$RAG_ROOT/log/rag-diagnostics.jsonl"

[qdrant]
url = "http://127.0.0.1:$RAG_QDRANT_PORT"
api_key_file = "$RAG_ROOT/state/qdrant.key"

[inference]
embed_url = "http://127.0.0.1:$((RAG_LLM_PORT_BASE + 1))"
rerank_url = "http://127.0.0.1:$((RAG_LLM_PORT_BASE + 2))"
describe_url = "http://127.0.0.1:$((RAG_LLM_PORT_BASE + 3))"
artifact_lock = "$RAG_BUNDLE/artifacts.lock.json"
runtime_dir = "$RAG_LAUNCH_DIR"

[extract]
docling_model_dir = "$RAG_BUNDLE/models/docling"
layout_model_dir = "$RAG_BUNDLE/models/PP-DocLayoutV2"
formula_model_dir = "$RAG_BUNDLE/models/PP-FormulaNet_plus-S"
tessdata_dir = "$RAG_BUNDLE/models/tessdata"

[limits]
max_source_bytes = 67108864
max_pdf_pages = 100
max_unpacked_bytes = 268435456
pool_limit = 32
default_min_score = 0.0
index_timeout_seconds = 900
search_timeout_seconds = 45
get_timeout_seconds = 3
EOF_RAG

cat > "$RAG_ROOT/mcp.toml" <<EOF_MCP
config_version = 1
grants = ["rag.index", "rag.read"]

[[modules]]
manifest = "$RAG_BUNDLE/rag-module.json"

[execution]
timeout_seconds = 960
max_concurrent_calls = 2
EOF_MCP

RAG_DEVICE_ARGS=()
if [ "$RAG_TARGET" = cuda132 ]; then
  RAG_DEVICE_ARGS=(--gpu "$RAG_GPU_UUID" --gpu-layers "$RAG_GPU_LAYERS")
fi
"$RAG_ROOT/venv/bin/mcp-rag-check" launch --config "$MCP_RAG_CONFIG" \
  --bundle "$RAG_BUNDLE" --profile "$RAG_TARGET" --runtime-dir "$RAG_LAUNCH_DIR" \
  --generator-url "$RAG_GENERATOR_URL" "${RAG_DEVICE_ARGS[@]}" || exit 1

"$RAG_ROOT/venv/bin/mcp-rag-check" preflight --config "$MCP_RAG_CONFIG" \
  --generator-url "$RAG_GENERATOR_URL" --profile "$RAG_TARGET" --runtime-dir "$RAG_LAUNCH_DIR" || exit 1

"$RAG_ROOT/venv/bin/mcp-rag-admin" --config "$MCP_RAG_CONFIG" \
  --mcp-config "$RAG_ROOT/mcp.toml" init || exit 1
"$RAG_ROOT/venv/bin/mcp-core" --config "$RAG_ROOT/mcp.toml" --port "$RAG_MCP_PORT"
```

`launch` сверяет хеши бинарников и модельных файлов с ведомостью, затем
собирает параметры фактического запуска четырёх ролей. Для CPU запускает
CPU-сборку с пустым `CUDA_VISIBLE_DEVICES`, `--device none` и
`--no-mmproj-offload` для описателя. Для CUDA запускает CUDA-сборку с
видимостью выбранного GPU, положительным offload слоёв каждой роли и
offload проектора описателя. Эмбеддинг использует `--embedding` и значение
`--pooling` из `[inference].artifact_lock`, `last` в базовой ведомости.
Реранкер использует `--reranking`, описатель — `--mmproj`, генератор — `--jinja`.
Все роли запускаются с `--offline`.

Для эмбеддингов `launch` строит канонический `embedding_profile` из
используемых артефактов, аргументов запуска и настроек подготовки текста в
доверенной ведомости. SHA-256 полного профиля даёт `profile_id`, который
передаётся как alias `rag-embed-<profile_id>`. Запись запуска в закрытом
`runtime_dir` привязывает роль, endpoint, PID, время старта, исполняемый
файл, хеш аргументов и идентификатор слушающего сокета. Освободите
занятый endpoint до запуска. Найденный на порту старый или чужой процесс
приводит к отказу. Лаунчер завершает при ошибке процессы своей попытки.
[Профиль и запись запуска](contracts/rag-tools.md)

Лаунчер собирает только разрешённые стартовые признаки загрузки модели,
offload и готовности. Он сохраняет эти признаки с привязкой к записи запуска.
После старта он отбрасывает вывод моделей и не сохраняет исходный поток логов.

Проверьте discovery на `http://127.0.0.1:<RAG_MCP_PORT>/mcp` для
`rag_index`, `rag_search` и `rag_get`. Для сценариев доступа повторите
запуск только `mcp-core` с нужным сочетанием `grants`, сохраняя модели
и инициализированный корпус.

## 5. Выполните проверку

В другой оболочке восстановите экспортированные параметры того же профиля
из разделов 2 и 4, включая `RAG_LAUNCH_DIR`, `RAG_GENERATOR_URL` и
`MCP_RAG_CONFIG`. `acceptance` проверяет действующую аттестацию запуска.

```bash
"$RAG_ROOT/venv/bin/mcp-rag-check" acceptance --config "$MCP_RAG_CONFIG" \
  --mcp-url "http://127.0.0.1:$RAG_MCP_PORT/mcp" --generator-url "$RAG_GENERATOR_URL" \
  --fixtures "$RAG_BUNDLE/fixtures" --profile "$RAG_TARGET" --runtime-dir "$RAG_LAUNCH_DIR" \
  --report "$RAG_ROOT/acceptance-$RAG_TARGET.json"
```

`preflight` подтверждает по `/proc` идентичность исполняемого файла, PID,
время старта и владение слушающим сокетом каждого endpoint. Проверка
сопоставляет наблюдаемые параметры процесса, запись запуска, доверенную
ведомость и alias полного профиля. Контрольные запросы и документы проходят
штатную подготовку текста. Их векторы сравниваются с эталонами из ведомости
в пределах заданных там допусков после проверки размерности и конечности
компонент. Проверяются также оценки контрольного пула и порядок релевантной
пары, ответ описателя на изображение и ответ генератора.
До инициализации корпуса успешный `preflight` атомарно сохраняет
`embedding-attestation.json` в `RAG_LAUNCH_DIR`. Аттестация действительна
для текущего процесса, записи запуска и ведомости.

Для Word проверка запускает одноразовую сессию Writer через системный
`python3-uno` внутри `bubblewrap` и проверяет запрет сетевого доступа,
выполнения макросов и обновления внешних ссылок. Ошибка изоляции завершает
проверку без запуска конвертера вне песочницы.

Протокол приёмки должен показать следующие результаты.

| Проверка | Ожидаемый результат |
|---|---|
| DOC/DOCX и Markdown | Покрытие SC-001 и SC-002. Сверяются нативные формулы и исходные вложения Word/MathType. Растровая формула DOC имеет отдельный ID с типом `formula`, исходный якорь Word, читаемое изображение и предупреждение об отсутствии нативной записи. Обычный рисунок того же DOC имеет тип `image`. Контрольные макрос и внешняя ссылка подтверждают запрет исполнения и обновления при конвертации. Проверяются Markdown и его вложения. |
| PDF с поворотом, обрезкой и сканами | Для страниц с поворотом `Rotate` и `CropBox` проверяются вырезанные области, разные ID inline/display формул и локатор из единственного поля `{"page": N}`. У отсканированной таблицы проверяются OCR текста ячеек и структура, включая объединения в эталоне. Смешанный PDF сохраняет связь всех материалов с исходной страницей. |
| Исходники и идентификаторы | Покрытие SC-001 и SC-007. Для длинных одноимённых перегрузок в одной области видимости сверяются исходные строки, общность `symbol_id` у фрагментов одной декларации и разные ID у разных деклараций. |
| Модуль → хост → модель | Хост получает настоящие результаты `rag_search` и `rag_get`, передаёт их локальной модели и получает контрольное значение с источником. |
| Нагрузка и релевантность | ≥100 файлов с активными версиями и ≥10 000 уникальных материалов. Выполняются 12 размеченных запросов SC-003, минимум 5 из 6 на каждом языке в top 5. Поиск ≤45 с, одна часть `rag_get` ≤3 с. Фиксируются размеры пула, длительности и аппаратный профиль. |
| Реранкинг | Один пул и известный исходный порядок, повышение кандидата, равные оценки, порог, пустой пул и недоступный реранкер. Выдача по непустому пулу требует всех оценок. |
| Эффективный профиль эмбеддингов | На тестовом корпусе после индексации с `last` перезапустите модели с тестовой `[inference].artifact_lock` для `mean`. Выполните успешный `preflight` с её эталонами. Убедитесь, что `profile_id` изменился, поиск сообщает о несовместимом поколении, а `init` возвращает ошибку несовместимости и сохраняет прежнее поколение. Уберите входные файлы, выполните `rebuild` из SQLite и проверьте поиск. При подстановке старого alias аттестация отклоняет подмену. |
| Аттестация запуска | Без `preflight` текущего процесса `init` и `rebuild` возвращают ошибку. Замена процесса с прежним профилем аннулирует аттестацию. Поиск сообщает об ошибке до повторного `preflight`, после которого прежнее совместимое поколение снова доступно. Чтение SQLite по ID доступно на всех этапах. |
| Отказы и права | Сценарии SC-004 и SC-006, включая потерю ответа после записи, неподтверждённую запись, параллельные снимки и восстановление без каталога входных файлов. По ID можно прочесть сохранённое при выключенных моделях или Qdrant. |
| Конкурирующий `prune-index` | Для импорта и `rebuild` отдельно приостановите операцию после записи точек перед `COMMIT` SQLite. Запустите `prune-index` и убедитесь, что он ждёт или возвращает занятость. Разрешите `COMMIT` и удержите блокировку на тестовом барьере после коммита. Очистка всё ещё ждёт или возвращает занятость. После освобождения блокировки и очистки проверьте доступность новой версии или поколения. Повторите с откатом и проверьте сохранность прежнего состояния. |
| Полный отчёт и бюджет выдачи | Индексируйте с обоими полномочиями файл, у которого замечания превышают бюджет краткого ответа. Соберите UTF-8 JSON из Base64-частей `view=report` и `view=full_metadata` по `offset` и сверьте с эталоном. Из полного отчёта прочитайте через `view=content` каждую пару `material_id` и `representation_id` неиндексируемого объекта из пропущенной части списка. Сверьте содержимое с эталоном. Повторите с одним `rag.index` и проверьте отсутствие `report_id` и отказ чтения. Измерьте ASCII JSON в обёртке ядра для отчёта и максимальной выдачи. Ответ укладывается в 1 МиБ, сырая часть — в 128 КиБ. |

Для проверки исключения возьмите ранее проиндексированный `sample.pdf`
и материал из его выдачи:

```bash
"$RAG_ROOT/venv/bin/mcp-rag-admin" --config "$MCP_RAG_CONFIG" \
  --mcp-config "$RAG_ROOT/mcp.toml" exclude --file sample.pdf
```

Следующий `rag_search` пропускает этот источник, а прежний `rag_get`
возвращает материал. Обычный `rag_index` отвечает `excluded`. Явный
`rag_index {"file":"sample.pdf","mode":"publish"}` возвращает источник
после полной обработки. Контрольная проверка включает перезапуск и
попытку чтения без полномочия.

## 6. Подтвердите профиль CUDA 13.2 отдельно

На сборочном стенде сверьте `nvcc --version`, `nvidia-smi` и
compute capability GPU. Для 13.2.0 требуется драйвер из профиля R1.
Соберите llama.cpp того же тега с включённым CUDA и выбранной
архитектурой, например для GPU с compute capability 8.6:

```bash
cmake -S ./llama.cpp-b12359 -B ./build-rag-cuda132 \
  -DGGML_CUDA=ON -DCMAKE_CUDA_COMPILER=/usr/local/cuda-13.2/bin/nvcc \
  -DCMAKE_CUDA_ARCHITECTURES=86 -DCMAKE_BUILD_TYPE=Release
cmake --build ./build-rag-cuda132 --target llama-server --parallel
```

В новой оболочке на целевом стенде выберите UUID из `nvidia-smi -L` и
положительное число слоёв по доступной VRAM для четырёх модельных ролей.
Задайте профиль и повторите разделы 2–5:

```bash
nvidia-smi -L
read -r -p 'UUID целевой видеокарты: ' RAG_GPU_UUID
read -r -p 'Число слоёв на GPU для каждой роли: ' RAG_GPU_LAYERS
export RAG_TARGET=cuda132 RAG_GPU_UUID RAG_GPU_LAYERS
```

Используйте тот же комплект контрольных входов и чистый каталог состояния
`rag-runtime-cuda132`. Службы CUDA используют порты 6433, 8191–8194 и 8776,
CPU — 6333, 8091–8094 и 8766. Индекс каждого прогона строится из контрольных
входов в своём каталоге состояния с фактическим профилем эмбеддингов.

Для зачёта CUDA `preflight` связывает каждую модельную роль по endpoint,
PID и времени старта с CUDA-бинарником и стартовым доказательством
положительного offload слоёв на выбранный GPU. Для описателя также нужен
признак запуска проектора на этом GPU. Неудачный запуск, CPU-процесс на
CUDA-порту и отсутствие любого доказательства приводят к отказу.
`acceptance` повторяет проверку связи перед прогоном и сохраняет
результаты двухъязычной релевантности, сквозного поиска и проверки
offload в `acceptance-cuda132.json`. В отчёте указаны использованные GPU,
драйвер, патч Toolkit и настройки.

Для остановки управляемых модельных процессов используйте каталог того
профиля, который требуется остановить. Команда сверяет идентификатор
процесса и время его старта:

```bash
"$RAG_ROOT/venv/bin/mcp-rag-check" stop --runtime-dir "$RAG_LAUNCH_DIR"
```
