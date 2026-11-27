"""Recoverable acme.sh state snapshots for migration within an AWS account."""

import json
import os
import shutil
import tempfile
from pathlib import Path

from .certificate import json_bytes
from .filesystem import read_regular, sync_directory, write_atomic

DIRECTORIES = ("config", "certificates", "work")


class Migration:
    def __init__(self, instance: Path) -> None:
        self.instance = instance
        self.path = instance / "migration-state"

    def begin(self, intent: dict) -> None:
        if self.path.exists():
            return
        scratch = Path(tempfile.mkdtemp(prefix=".migration-", dir=self.instance))
        try:
            (scratch / "original").mkdir(mode=0o700)
            (scratch / "discard").mkdir(mode=0o700)
            for name in DIRECTORIES:
                shutil.copytree(self.instance / name, scratch / "original" / name, symlinks=True)
            for folder, _, files in os.walk(scratch / "original", topdown=False):
                directory = Path(folder)
                for name in files:
                    path = directory / name
                    if not path.is_symlink():
                        with path.open("rb") as stream:
                            os.fsync(stream.fileno())
                sync_directory(directory)
            write_atomic(scratch / "intent.json", json_bytes(intent))
            os.rename(scratch, self.path)
            sync_directory(self.instance)
        finally:
            if scratch.exists():
                shutil.rmtree(scratch)

    def intent(self) -> dict:
        return json.loads(read_regular(self.path / "intent.json", 4096, private=True))

    def restore(self) -> None:
        originals = self.path / "original"
        discard = self.path / "discard"
        for name in DIRECTORIES:
            source = originals / name
            if source.exists():
                live = self.instance / name
                if live.exists():
                    if (discard / name).exists():
                        # Instance setup may have recreated an empty directory during recovery.
                        live.rmdir()
                    else:
                        os.rename(live, discard / name)
                        sync_directory(discard)
                os.rename(source, live)
                sync_directory(self.instance)
                sync_directory(originals)
        self.finish()

    def finish(self) -> None:
        # Detach the snapshot before removing it, so misinterpreting an
        # interrupted removal as a pending restoration is impossible.
        if self.path.exists():
            retired = self.instance / ".migration-retired"
            os.rename(self.path, retired)
            sync_directory(self.instance)
        for path in self.instance.glob(".migration-*"):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
        sync_directory(self.instance)
