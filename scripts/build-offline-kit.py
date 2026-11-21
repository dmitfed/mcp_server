#!/usr/bin/env python3
"""Собрать комплект для офлайн-установки на Debian с зафиксированными хешами."""

import argparse
from email.parser import BytesParser
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import sys
from zipfile import ZipFile


PROJECT = Path(__file__).resolve().parents[1]
ASSETS = {
    "engine/llama-b12359-bin-ubuntu-x64.tar.gz": "0bc1f091e8916102b6d8f2cf944233a620e90a0f856c72d0ce41150c65c2e72e",
    "model/Qwen3-4B-Q4_K_M.gguf": "7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5",
}


def sha256(path: Path) -> str:
    """Вычислить SHA-256 потоковым чтением, в том числе для большого файла GGUF."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_lock(wheelhouse: Path, output: Path) -> None:
    """Зафиксировать имена, точные версии и SHA-256 из собранного wheelhouse.

    Имена и версии берутся из метаданных внутри wheel. Для каждого пакета
    допускается один wheel, а wheel самого проекта должен быть в комплекте.
    Сортировка делает порядок строк стабильным.
    """
    requirements = {}
    for wheel in sorted(wheelhouse.glob("*.whl")):
        with ZipFile(wheel) as archive:
            (metadata,) = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            info = BytesParser().parsebytes(archive.read(metadata), headersonly=True)
        name = re.sub(r"[-_.]+", "-", info["Name"]).lower()
        if name in requirements:
            raise ValueError(f"wheelhouse contains more than one wheel for {name}")
        requirements[name] = f"{name}=={info['Version']} --hash=sha256:{sha256(wheel)}"
    if "mcp-offline-core" not in requirements:
        raise ValueError("wheelhouse does not contain the project wheel")
    output.write_text("".join(requirements[name] + "\n" for name in sorted(requirements)))


def main() -> None:
    """Собрать новый wheelhouse и записать проверяемые данные для переноса.

    Режимы `core` и `modules` собирают промежуточные наборы приёмки.
    Режим `release` дополнительно проверяет и копирует архив движка и GGUF.
    Ведомость SHA-256 формируется после копирования всех файлов.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--image-id", required=True, help="identifier of the prepared Debian base image")
    parser.add_argument("--stage", choices=("core", "modules", "release"), default="release")
    parser.add_argument("--inputs", type=Path, default=PROJECT / "offline-kit-inputs")
    arguments = parser.parse_args()
    destination = arguments.destination.resolve()
    if arguments.stage == "release":
        for name, digest in ASSETS.items():
            if sha256(arguments.inputs / name) != digest:
                parser.error(f"checksum mismatch for {name}")

    # Новый каталог исключает смешивание wheel разных сборок с той же версией.
    destination.mkdir()
    wheelhouse = destination / "wheelhouse"
    wheelhouse.mkdir()
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-cache-dir", "--only-binary=:all:",
         "--wheel-dir", str(wheelhouse), str(PROJECT)],
        check=True,
    )
    write_lock(wheelhouse, destination / "requirements.lock")

    image_packages = subprocess.check_output(
        ["dpkg-query", "-W", "-f=${binary:Package}=${Version}\n"], text=True,
    )
    release = Path("/etc/os-release").read_text()
    (destination / "base-image.txt").write_text(
        f"Image: {arguments.image_id}\nPython: {sys.version.split()[0]}\n{release}\n{image_packages}"
    )
    (destination / "examples").mkdir()
    for name in ("empty.toml",) if arguments.stage == "core" else ("empty.toml", "demo.toml", "demo-module.json"):
        shutil.copy2(PROJECT / "examples" / name, destination / "examples" / name)
    if arguments.stage == "core":
        (destination / "tests").mkdir()
        shutil.copy2(PROJECT / "tests/test_mcp_http.py", destination / "tests/test_mcp_http.py")
        fixtures = PROJECT / "tests/fixtures"
        if fixtures.is_dir():
            shutil.copytree(fixtures, destination / "tests/fixtures", ignore=shutil.ignore_patterns("__pycache__"))
    else:
        shutil.copytree(PROJECT / "tests", destination / "tests", ignore=shutil.ignore_patterns("__pycache__"))
    if arguments.stage == "release":
        for name in ASSETS:
            target = destination / name
            target.parent.mkdir(exist_ok=True)
            shutil.copy2(arguments.inputs / name, target)

    files = sorted(path for path in destination.rglob("*") if path.is_file())
    (destination / "SHA256SUMS").write_text(
        "".join(f"{sha256(path)}  {path.relative_to(destination).as_posix()}\n" for path in files)
    )


if __name__ == "__main__":
    main()
