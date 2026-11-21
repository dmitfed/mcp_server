"""Чтение текстового слоя локальных PDF для MCP-инструмента `pdf_read`."""

import os
from pathlib import Path
import re
import stat

from pypdf import PdfReader


MAX_PDF_BYTES = 8 * 1024 * 1024
MAX_CHARS_PER_PAGE = 4_000


def read_pdf(arguments, context):
    """Вернуть текст выбранных страниц локального PDF.

    Страницы нумеруются с единицы. Поле `next_page` позволяет продолжить
    чтение с первой страницы, которая ещё не вошла в ответ.
    """
    if "pdf.read" not in context.grants:
        raise PermissionError("Требуется полномочие pdf.read")

    filename = arguments["file"]
    if re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,100}\.pdf", filename) is None:
        raise ValueError("Ожидается имя PDF из каталога документов")

    start_page = int(arguments.get("start_page", 1))
    max_pages = int(arguments.get("max_pages", 3))
    root = Path(os.environ["MCP_PDF_ROOT"]).resolve(strict=True)
    descriptor = os.open(root / filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as document:
        info = os.fstat(document.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PDF_BYTES:
            raise ValueError("Требуется обычный PDF-файл размером до 8 МиБ")

        reader = PdfReader(document, strict=True)
        if reader.is_encrypted:
            raise ValueError("Поддерживаются PDF без шифрования")

        page_count = len(reader.pages)
        if start_page > page_count:
            raise ValueError("Начальная страница находится за пределами PDF")
        last_page = min(start_page + max_pages - 1, page_count)

        pages = []
        for number in range(start_page, last_page + 1):
            text = reader.pages[number - 1].extract_text().strip()
            pages.append({
                "page": number,
                "text": text[:MAX_CHARS_PER_PAGE],
                "truncated": len(text) > MAX_CHARS_PER_PAGE,
            })

    return {
        "file": filename,
        "page_count": page_count,
        "pages": pages,
        "next_page": last_page + 1 if last_page < page_count else None,
    }
