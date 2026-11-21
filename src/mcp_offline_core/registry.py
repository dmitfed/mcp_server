"""Загрузка конфигурации и проверка манифестов локальных модулей."""

from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import tomllib

from jsonschema import Draft202012Validator, SchemaError
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012


class ConfigurationError(ValueError):
    """Ошибка конфигурации с сообщением для диагностики запуска."""


@dataclass(frozen=True)
class Configuration:
    """Проверенные пути манифестов, ограничения вызовов и полномочия оператора."""

    manifests: tuple[Path, ...]
    disabled_tools: frozenset[str]
    grants: frozenset[str]
    timeout_seconds: float
    max_concurrent_calls: int


@dataclass(frozen=True)
class RegisteredTool:
    """Метаданные из манифеста и валидатор аргументов для повторных вызовов."""

    name: str
    description: str
    handler: str
    schema: dict
    validator: Draft202012Validator


_TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_HANDLER = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", re.ASCII)


def _string_list(value: object, field: str) -> tuple[str, ...]:
    """Проверить список строк TOML и указать имя поля в случае ошибки.

    Пустые и пробельные элементы отклоняются. Исходное написание
    остальных элементов сохраняется для точного сравнения имён.
    """
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ConfigurationError(f"{field} must be a list of nonempty strings")
    return tuple(value)


def load_configuration(path: Path) -> Configuration:
    """Прочитать TOML v1 и подготовить параметры для запуска экземпляра.

    Относительные пути манифестов отсчитываются от каталога конфига.
    Если блок исполнения отсутствует, действуют таймаут 10 секунд и четыре
    слота. Содержимое файла исключается из диагностических сообщений.
    """
    try:
        with path.open("rb") as stream:
            config = tomllib.load(stream)
    except OSError:
        raise ConfigurationError(f"cannot read configuration {str(path)!r}") from None
    except (ValueError, UnicodeError):
        raise ConfigurationError(f"invalid TOML in configuration {str(path)!r}") from None

    version = config.get("config_version")
    if type(version) is not int or version != 1:
        raise ConfigurationError("config_version must be 1")
    if config.keys() - {"config_version", "modules", "disabled_tools", "grants", "execution"}:
        raise ConfigurationError("unexpected top-level configuration field")
    modules = config.get("modules", [])
    if not isinstance(modules, list):
        raise ConfigurationError("modules must be a list")
    manifests = []
    for index, module in enumerate(modules):
        if not isinstance(module, dict) or module.keys() != {"manifest"}:
            raise ConfigurationError(f"modules[{index}] must specify a manifest")
        filename = module["manifest"]
        if not isinstance(filename, str) or not filename.strip() or "\x00" in filename:
            raise ConfigurationError(f"modules[{index}].manifest must be a path")
        manifests.append(path.absolute().parent / filename)
    execution = config.get("execution", {})
    if not isinstance(execution, dict) or execution.keys() - {"timeout_seconds", "max_concurrent_calls"}:
        raise ConfigurationError("invalid execution configuration")
    timeout = execution.get("timeout_seconds", 10)
    if type(timeout) not in (int, float) or not 0 < timeout <= math.nextafter(math.inf, 0):
        raise ConfigurationError("execution.timeout_seconds must be positive and finite")
    slots = execution.get("max_concurrent_calls", 4)
    if type(slots) is not int or slots < 1:
        raise ConfigurationError("execution.max_concurrent_calls must be a positive integer")
    return Configuration(
        tuple(manifests),
        frozenset(_string_list(config.get("disabled_tools", []), "disabled_tools")),
        frozenset(_string_list(config.get("grants", []), "grants")),
        float(timeout),
        slots,
    )


def _reject_nonfinite(value: str):
    """Отклонить NaN и бесконечности, которые стандартный декодер Python допускает по умолчанию."""
    raise ValueError(f"{value} is not a JSON value")


def _check_schema_references(schema, resolver) -> None:
    """Проверить разрешимость локальных ссылок схемы Draft 2020-12.

    Обход ограничен вложенными схемами, определёнными этим диалектом. На
    каждом уровне учитывается область видимости ресурса, а ссылки с адресом
    вне текущего документа отклоняются до запуска сервера.
    """
    if isinstance(schema, bool):
        return
    if "$schema" in schema and schema["$schema"] != "https://json-schema.org/draft/2020-12/schema":
        raise ValueError("unsupported schema dialect")
    for keyword in ("$ref", "$dynamicRef"):
        if keyword in schema:
            ref = schema[keyword]
            if ref and not ref.startswith("#"):
                raise ValueError("reference outside the document")
            resolver.lookup(ref)
    for subschema in DRAFT202012.subresources_of(schema):
        resource = Resource(subschema, DRAFT202012)
        _check_schema_references(subschema, resolver.in_subresource(resource))


def _make_tool(raw, source: Path) -> RegisteredTool:
    """Проверить одно объявление инструмента и подготовить офлайн-валидатор.

    Адрес обработчика проверяется как строка. Импорт функции произойдёт
    при вызове в дочернем процессе. Корнем входной схемы должен быть объект,
    чтобы формат соответствовал аргументам MCP.
    """
    if not isinstance(raw, dict):
        raise ConfigurationError(f"invalid tool in {str(source)!r}")
    name = raw.get("name")
    if not isinstance(name, str) or _TOOL_NAME.fullmatch(name) is None:
        raise ConfigurationError(f"invalid tool name in {str(source)!r}")
    description = raw.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ConfigurationError(f"tool {name!r} has no description")
    handler = raw.get("handler")
    if not isinstance(handler, str) or _HANDLER.fullmatch(handler) is None:
        raise ConfigurationError(f"tool {name!r} has an invalid handler address")
    schema = raw.get("input_schema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ConfigurationError(f"tool {name!r} input_schema must describe an object")
    try:
        Draft202012Validator.check_schema(schema)
        # Пустой реестр без функции загрузки исключает сетевое разрешение ссылок.
        offline = Registry()
        resolver = offline.resolver_with_root(Resource(schema, DRAFT202012))
        _check_schema_references(schema, resolver)
    except (SchemaError, Unresolvable, ValueError, RecursionError):
        raise ConfigurationError(f"tool {name!r} has an invalid or nonlocal input_schema") from None
    return RegisteredTool(name, description, handler, schema, Draft202012Validator(schema, registry=offline))


def load_registry(config: Configuration) -> dict[str, RegisteredTool]:
    """Загрузить явно перечисленные манифесты и собрать общий каталог по имени.

    Уникальность идентификаторов модулей и имён инструментов проверяется
    во всех манифестах, включая отключённые инструменты. Отключённые имена
    остаются в реестре, а фильтрацию при публикации выполняет сервер.
    """
    tools = {}
    modules = set()
    for source in config.manifests:
        try:
            raw = json.loads(source.read_text(encoding="utf-8"), parse_constant=_reject_nonfinite)
        except (OSError, ValueError, RecursionError):
            raise ConfigurationError(f"cannot read a valid manifest from {str(source)!r}") from None
        if not isinstance(raw, dict) or type(raw.get("contract_version")) is not int or raw["contract_version"] != 1:
            raise ConfigurationError(f"contract_version must be 1 in {str(source)!r}")
        module_id = raw.get("module_id")
        if not isinstance(module_id, str) or not module_id.strip():
            raise ConfigurationError(f"invalid module_id in {str(source)!r}")
        if module_id in modules:
            raise ConfigurationError(f"duplicate module_id {module_id!r}")
        modules.add(module_id)
        definitions = raw.get("tools")
        if not isinstance(definitions, list):
            raise ConfigurationError(f"tools must be a list in {str(source)!r}")
        for definition in definitions:
            tool = _make_tool(definition, source)
            if tool.name in tools:
                raise ConfigurationError(f"duplicate tool name {tool.name!r}")
            tools[tool.name] = tool
    if unknown := config.disabled_tools - tools.keys():
        raise ConfigurationError(f"unknown disabled tool {min(unknown)!r}")
    return tools
