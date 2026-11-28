"""Execute trusted local certificate update hooks."""

import os
import stat
import subprocess
from pathlib import Path


def hook_paths(directory: Path) -> list[Path]:
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return []
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError("The hook directory must be owned by the client user and protected")
    result = []
    for path in sorted(directory.iterdir()):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or not info.st_mode & stat.S_IXUSR:
            continue
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ValueError(f"Unsafe ownership or permissions on hook {path.name}")
        result.append(path)
    return result


def run_hook(root: Path, path: Path, event: str, identifiers: list[str]) -> None:
    try:
        subprocess.run(
            [str(path)],
            cwd=root,
            env={
                "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                "CLM_CLIENT_ROOT": str(root),
                "CLM_HOOK_EVENT": event,
                "CLM_UPDATED_CERTIFICATES": "\n".join(identifiers),
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
            check=True,
            umask=0o077,
        )
    except (OSError, subprocess.SubprocessError):
        raise ValueError(f"Certificate hook {path.name} failed during {event}") from None
