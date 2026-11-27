"""Prepare the selected application and its command entry points."""

import shutil
import sys
import tomllib
from pathlib import Path


def stage(source: Path, role: str, workspace: Path, interpreter: Path) -> None:
    package = {"server": "clm_server", "client": "clm_client"}[role]
    metadata = tomllib.loads((source / "pyproject.toml").read_text())["project"]
    application = workspace / "application" / package
    application.mkdir(parents=True)
    for module in (source / "src" / package).glob("*.py"):
        shutil.copyfile(module, application / module.name)
    with (application / "__init__.py").open("a") as module:
        module.write(f"\n__version__ = {metadata['version']!r}\n")

    commands = workspace / "bin"
    commands.mkdir()
    for name, entry_point in metadata["scripts"].items():
        module, function = entry_point.split(":")
        if module.split(".")[0] == package:
            (commands / name).write_text(
                f"#!{interpreter}\n"
                f"from {module} import {function}\n"
                f"raise SystemExit({function}())\n"
            )


if __name__ == "__main__":
    repository, product, work, python = sys.argv[1:]
    stage(Path(repository), product, Path(work), Path(python))
