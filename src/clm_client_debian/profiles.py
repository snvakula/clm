"""Run locally configured service commands without a shell."""

import logging
import os
import re
import subprocess
from pathlib import Path

from .config import load_config
from .logging import progress


class Profiles:
    def __init__(self, root: Path, logger: logging.Logger) -> None:
        self.directory = root / "config/services.d"
        self.logger = logger

    def load(self, name: str) -> dict:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", name):
            raise ValueError("Invalid local service profile name")
        path = self.directory / f"{name}.yaml"
        for item in (self.directory, path):
            info = item.lstat()
            if item.is_symlink() or info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022:
                raise ValueError(f"Unsafe service profile permissions for {name}")
        profile = load_config(path)
        if (
            not {"name", "check", "reload"} <= set(profile)
            or set(profile) - {"name", "check", "reload", "verify", "timeout"}
            or profile["name"] != name
            or type(profile.get("timeout", 30)) is not int
            or not 1 <= profile.get("timeout", 30) <= 300
        ):
            raise ValueError(f"Invalid service profile for {name}")
        for operation in ("check", "reload", "verify"):
            if operation in profile and (
                not isinstance(profile[operation], list)
                or not profile[operation]
                or any(
                    not isinstance(arg, str) or not arg or "\x00" in arg
                    for arg in profile[operation]
                )
            ):
                raise ValueError(f"Invalid service command for {name}")
        return profile

    def execute(self, profile: dict, operation: str) -> None:
        if operation not in profile:
            return
        name = profile["name"]
        descriptions = {
            "check": "Checking configuration for",
            "reload": "Reloading",
            "verify": "Verifying",
        }
        progress(self.logger, "%s %s", descriptions[operation], name)
        try:
            subprocess.run(
                profile[operation],
                cwd="/",
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=profile.get("timeout", 30),
                check=True,
                umask=0o077,
            )
        except (OSError, subprocess.SubprocessError):
            raise ValueError(f"Service {operation} failed for {name}") from None
        self.logger.info("Service %s completed for %s", operation, name)
