"""Распаковка локальных ZIP-архивов для MCP-инструмента `zip_extract`."""

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
    """Распаковать один ZIP из `inbox` и вернуть относительный каталог и список файлов.

    Для каждого вызова выделяется собственная папка. Готовые файлы публикуются
    под именем `files` после успешной записи всех записей архива.
    """
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
