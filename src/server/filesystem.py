"""Durable filesystem changes within trusted application directories."""

import os
import stat
import tempfile
from pathlib import Path


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_atomic(
    path: Path, content: bytes, mode: int = 0o600, *, owner: tuple[int, int] | None = None
) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if owner is not None:
                os.fchown(stream.fileno(), *owner)
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def read_regular(path: Path, maximum: int, *, private: bool = False) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError("Invalid file type or size")
        if private and info.st_mode & 0o077:
            raise ValueError("Credentials must be readable only by their owner")
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("File exceeds the permitted size")
    return data
