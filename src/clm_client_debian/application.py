"""Synchronise certificates from CLM and run local update hooks."""

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .certificate import (
    MAX_FILE,
    PEMS,
    TRUST,
    json_bytes,
    parse_manifest,
    read_material,
    unpack_bundle,
    utc,
    validate,
)
from .config import load_config
from .filesystem import read_regular, write_atomic
from .generations import Generations
from .hooks import hook_paths, run_hook
from .logging import progress
from .remote import Remote


class Client:
    def __init__(self, root: Path, logger: logging.Logger, *, trust: Path = TRUST) -> None:
        self.root = root
        self.trust = trust
        self.logger = logger
        self.files = Generations(root / "certificates")
        self.state_file = root / "data/state.json"
        self.config_file = root / "config/client.yaml"
        self.hooks = root / "config/hooks.d"

    def state(self) -> dict:
        try:
            data = read_regular(self.state_file, 4 * 1024 * 1024, private=True)
        except FileNotFoundError:
            return {"certificates": {}, "pending": None, "last_update": None, "last_error": None}
        value = json.loads(data)
        if not isinstance(value, dict) or not isinstance(value.get("certificates"), dict):
            raise ValueError("Invalid local certificate state")
        return value

    def save(self, state: dict) -> None:
        write_atomic(self.state_file, json_bytes(state))

    def remote(self) -> Remote:
        settings = load_config(self.config_file)
        server, token = settings.get("server"), settings.get("token")
        timeout = settings.get("request_timeout", 20)
        if set(settings) - {"server", "token", "request_timeout"}:
            raise ValueError("Unexpected setting in client.yaml")
        if not isinstance(server, str) or not server:
            raise ValueError("Set the CLM server URL in client.yaml")
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise ValueError("Set a valid client token in client.yaml")
        if type(timeout) is not int or not 1 <= timeout <= 120:
            raise ValueError("Invalid client request timeout")
        return Remote(server, token, timeout=timeout)

    def _rollback(self, state: dict) -> None:
        pending = state["pending"]
        progress(self.logger, "Restoring the previous certificate state")
        if pending["switching"]:
            for identifier, change in pending["changes"].items():
                if self.files.current(identifier) != change["old"]:
                    self.files.activate(identifier, change["old"])
        attempted = pending.get("attempted_hooks", [])
        if attempted:
            paths = {path.name: path for path in hook_paths(self.hooks)}
            failures = []
            for name in attempted:
                if name in paths:
                    try:
                        run_hook(self.root, paths[name], "rollback", pending["updated"])
                    except ValueError as error:
                        failures.append(str(error))
            if failures:
                raise ValueError("; ".join(failures))
        for identifier in pending["changes"]:
            row = state["certificates"].get(identifier) or {}
            self.files.clean(identifier, {row.get("version"), row.get("previous")})
        state["pending"] = None
        self.save(state)
        self.logger.warning("Restored previous certificate state")

    def recover(self, state: dict) -> None:
        if state["pending"]:
            try:
                self._rollback(state)
            except (OSError, ValueError):
                raise ValueError(
                    f"Rollback requires recovery using {self.state_file} and {self.files.root}"
                ) from None
        for identifier, row in state["certificates"].items():
            current = self.files.current(identifier)
            if current != row["version"]:
                raise ValueError(f"Local repository generation differs from state for {identifier}")
            self.files.clean(identifier, {row["version"], row["previous"]})
        for identifier in state.get("removals", []):
            if self.files.base(identifier).is_dir():
                self.files.activate(identifier, None)
                self.files.clean(identifier, set())
        if state.get("removals"):
            state.pop("removals")
            self.save(state)

    def inspect(self, state: dict, identifier: str) -> dict[str, Any]:
        row = state["certificates"].get(identifier)
        if row is None:
            raise ValueError("Certificate is not installed")
        live = self.files.base(identifier) / "current"
        directory = self.files.directory(identifier, row["version"])
        manifest = parse_manifest(read_regular(directory / "manifest.json", MAX_FILE), identifier)
        return {
            **manifest,
            "certificate_path": str(live / "fullchain.pem"),
            "private_key_path": str(live / "privkey.pem"),
        }

    def update(self, state: dict) -> tuple[int, int]:
        self.recover(state)
        pending = {"switching": False, "changes": {}, "updated": [], "attempted_hooks": []}
        registrations = dict(state["certificates"])
        try:
            remote = self.remote()
            progress(self.logger, "Fetching all assigned certificates from the CLM server")
            identifiers = remote.available()
            for identifier in identifiers:
                row = registrations.get(identifier)
                progress(self.logger, "Checking the current generation of %s", identifier)
                incoming = remote.manifest(identifier)
                number = incoming["version"]
                material = None
                if row:
                    installed = row["version"]
                    if number < installed:
                        raise ValueError(f"Server reported an older generation for {identifier}")
                    try:
                        local_files = read_material(self.files.directory(identifier, installed))
                        local = validate(
                            identifier,
                            installed,
                            local_files,
                            self.root / "run",
                            self.trust,
                        )
                    except (OSError, ValueError):
                        local = None
                    if local == incoming:
                        progress(self.logger, "%s is already at generation %s", identifier, number)
                        continue
                    if number == installed:
                        raise ValueError(
                            f"Installed generation requires local recovery for {identifier}"
                        )
                    if local is not None and {**local, "version": number} == incoming:
                        material = local_files
                if material is None:
                    progress(self.logger, "Downloading generation %s for %s", number, identifier)
                    archive = unpack_bundle(remote.bundle(identifier, number))
                    bundled = parse_manifest(archive["manifest.json"], identifier)
                    material = {name: archive[name] for name in PEMS}
                    progress(self.logger, "Validating the certificate and key for %s", identifier)
                    verified = validate(identifier, number, material, self.root / "run", self.trust)
                    if verified != bundled or bundled != incoming:
                        raise ValueError(
                            f"Certificate bundle does not match its manifest for {identifier}"
                        )
                    pending["updated"].append(identifier)
                else:
                    archive = {**material, "manifest.json": json_bytes(incoming)}
                old = row["version"] if row else None
                pending["changes"][identifier] = {"old": old, "new": number}
                state["pending"] = pending
                self.save(state)
                self.files.stage(identifier, number, archive)
                registrations[identifier] = {"version": number, "previous": old}

            if pending["changes"]:
                pending["switching"] = True
                self.save(state)
                for identifier, change in pending["changes"].items():
                    progress(
                        self.logger, "Activating generation %s for %s", change["new"], identifier
                    )
                    self.files.activate(identifier, change["new"])
            if pending["updated"]:
                for hook in hook_paths(self.hooks):
                    progress(self.logger, "Running certificate hook %s", hook.name)
                    pending["attempted_hooks"].append(hook.name)
                    self.save(state)
                    run_hook(self.root, hook, "updated", pending["updated"])
            completed = {
                **state,
                "pending": None,
                "certificates": registrations,
                "last_update": utc(datetime.now(UTC)),
                "last_error": None,
            }
            self.save(completed)
        except (OSError, ValueError) as exc:
            detail = str(exc) if type(exc) is ValueError else "Local certificate update I/O failed"
            durable = self.state()
            self.recover(durable)
            durable["last_error"] = detail
            self.save(durable)
            raise ValueError(detail) from None
        state.update(completed)
        self.recover(state)
        return len(identifiers), len(pending["changes"])
