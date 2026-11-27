"""Immutable certificate generations and atomic current links."""

import os
import re
import shutil
import tempfile
from pathlib import Path

from .certificate import FILES
from .filesystem import sync_directory, write_atomic
from .identity import storage_path


class Generations:
    def __init__(self, root: Path) -> None:
        self.root = root

    def base(self, identifier: str) -> Path:
        return self.root.joinpath(*storage_path(identifier))

    def directory(self, identifier: str, version: int) -> Path:
        return self.base(identifier) / "versions" / f"{version:06d}"

    def _mkdir(self, path: Path) -> None:
        if path == self.root:
            return
        self._mkdir(path.parent)
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            if path.is_symlink() or not path.is_dir():
                raise
        else:
            sync_directory(path.parent)

    def stage(self, identifier: str, version: int, files: dict[str, bytes]) -> None:
        if set(files) != set(FILES):
            raise ValueError("Unexpected generation files")
        target = self.directory(identifier, version)
        self._mkdir(target.parent)
        temporary = Path(tempfile.mkdtemp(prefix=".stage-", dir=target.parent))
        try:
            for name, data in files.items():
                write_atomic(temporary / name, data)
            if target.exists():
                raise FileExistsError("Certificate generation already exists")
            os.rename(temporary, target)
            sync_directory(target.parent)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def current(self, identifier: str) -> int | None:
        try:
            link = os.readlink(self.base(identifier) / "current")
        except FileNotFoundError:
            return None
        if not re.fullmatch(r"versions/[0-9]{6,19}", link):
            raise ValueError("Invalid current generation link")
        version = int(link.split("/")[1])
        if link != f"versions/{version:06d}" or not 0 < version < 2**63:
            raise ValueError("Invalid current generation link")
        directory = self.directory(identifier, version)
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("Current certificate generation is missing")
        return version

    def activate(self, identifier: str, version: int | None) -> None:
        base = self.base(identifier)
        if version is None:
            (base / "current").unlink(missing_ok=True)
        else:
            target = self.directory(identifier, version)
            if target.is_symlink() or not target.is_dir():
                raise ValueError("Certificate generation is missing")
            link = base / ".next"
            link.unlink(missing_ok=True)
            link.symlink_to(target.relative_to(base))
            os.replace(link, base / "current")
        sync_directory(base)

    def clean(self, identifier: str, keep: set[int | None]) -> None:
        versions = self.base(identifier) / "versions"
        if versions.is_dir():
            for path in versions.iterdir():
                if path.name.startswith(".stage-") or (
                    path.name.isascii() and path.name.isdecimal() and int(path.name) not in keep
                ):
                    shutil.rmtree(path)
            sync_directory(versions)
