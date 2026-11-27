"""Client configuration, certificate installation and recoverable service activation."""

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clm_common.certificate import (
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
from clm_common.config import load_config
from clm_common.filesystem import read_regular, write_atomic
from clm_common.generations import Generations

from .profiles import Profiles
from .remote import Remote, endpoint


class Client:
    def __init__(self, root: Path, logger: logging.Logger, *, trust: Path = TRUST) -> None:
        self.root = root
        self.trust = trust
        self.logger = logger
        self.files = Generations(root / "certificates")
        self.profiles = Profiles(root, logger)
        self.state_file = root / "data/state.json"
        self.credentials = root / "config/credentials"

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

    def _remote(self, server: str, token: str) -> Remote:
        settings = load_config(self.root / "config/client.yaml")
        timeout = settings.get("request_timeout", 20)
        if set(settings) - {"request_timeout"} or type(timeout) is not int or not 1 <= timeout <= 120:
            raise ValueError("Invalid client request timeout")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise ValueError("Invalid client access token")
        return Remote(server, token, timeout=timeout)

    def remote(self) -> Remote:
        try:
            value = json.loads(read_regular(self.credentials, 4096, private=True))
        except (UnicodeError, ValueError):
            raise ValueError("Invalid client credentials") from None
        if (
            not isinstance(value, dict)
            or set(value) != {"server", "token"}
            or not all(isinstance(item, str) for item in value.values())
        ):
            raise ValueError("Invalid client credentials")
        return self._remote(value["server"], value["token"])

    def initialise(self, server: str, token: str) -> str:
        url = endpoint(server)
        name = self._remote(url, token).client()
        if self.credentials.exists() and self.state()["certificates"] and url != self.remote().server:
            raise ValueError("Remove installed certificates before changing the CLM server")
        write_atomic(self.credentials, json_bytes({"server": url, "token": token}))
        return name

    def _rollback(self, state: dict) -> None:
        pending = state["pending"]
        affected = set()
        # Reload conservatively even when a previous recovery already restored the links.
        if pending["switching"]:
            for identifier, change in pending["changes"].items():
                if self.files.current(identifier) != change["old"]:
                    self.files.activate(identifier, change["old"])
                affected.update(change["services"])
            for name in sorted(affected):
                self.profiles.execute(self.profiles.load(name), "check")
            for name in sorted(affected):
                profile = self.profiles.load(name)
                self.profiles.execute(profile, "reload")
                self.profiles.execute(profile, "verify")
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

    def remove(self, state: dict, identifier: str, force: bool) -> None:
        row = state["certificates"].get(identifier)
        if row is None:
            raise ValueError("Certificate is not installed")
        if row["services"] and not force:
            raise ValueError("Certificate is associated with local services; use --force")
        state["certificates"].pop(identifier)
        state["removals"] = [identifier]
        self.save(state)
        self.recover(state)
        self.logger.info("Removed certificate %s", identifier)

    def inspect(self, state: dict, identifier: str) -> dict[str, Any]:
        row = state["certificates"].get(identifier)
        if row is None:
            raise ValueError("Certificate is not installed")
        live = self.files.base(identifier) / "current"
        directory = self.files.directory(identifier, row["version"])
        manifest = parse_manifest(read_regular(directory / "manifest.json", MAX_FILE), identifier)
        return {
            **manifest, "services": row["services"],
            "certificate_path": str(live / "fullchain.pem"),
            "private_key_path": str(live / "privkey.pem"),
        }

    def apply(
        self, state: dict, identifiers: list[str], *, service_names: list[str] | None = None,
    ) -> None:
        if not identifiers:
            return
        remote = self.remote()
        pending = {"switching": False, "changes": {}}
        registrations = dict(state["certificates"])
        profiles = {}
        try:
            for identifier in identifiers:
                row = state["certificates"].get(identifier, {})
                services = sorted(set(row.get("services", ())) | set(service_names or ()))
                for name in services:
                    if name not in profiles:
                        profiles[name] = self.profiles.load(name)
                incoming = remote.manifest(identifier)
                number = incoming["version"]
                if row:
                    directory = self.files.directory(identifier, row["version"])
                    try:
                        local = validate(identifier, row["version"], read_material(directory),
                                         self.root / "run", self.trust)
                    except (OSError, ValueError):
                        local = None
                    if number < row["version"]:
                        raise ValueError(f"Server reported an older generation for {identifier}")
                    if local == incoming:
                        registrations[identifier] = {**row, "services": services}
                        continue
                    if number == row["version"]:
                        raise ValueError(f"Installed generation requires local recovery for {identifier}")
                archive = unpack_bundle(remote.bundle(identifier, number))
                bundled = parse_manifest(archive["manifest.json"], identifier)
                material = {name: archive[name] for name in PEMS}
                verified = validate(identifier, number, material, self.root / "run", self.trust)
                if verified != bundled or bundled != incoming:
                    raise ValueError(f"Certificate bundle does not match its manifest for {identifier}")
                pending["changes"][identifier] = {
                    "old": row.get("version"), "new": number, "services": services,
                }
                state["pending"] = pending
                self.save(state)
                self.files.stage(identifier, number, archive)
                registrations[identifier] = {
                    "version": number, "previous": row.get("version"), "services": services,
                }
            if pending["changes"]:
                pending["switching"] = True
                self.save(state)
                for identifier, change in pending["changes"].items():
                    self.files.activate(identifier, change["new"])
                affected = sorted({
                    name for change in pending["changes"].values() for name in change["services"]
                })
                for name in affected:
                    self.profiles.execute(profiles[name], "check")
                for name in affected:
                    self.profiles.execute(profiles[name], "reload")
                    self.profiles.execute(profiles[name], "verify")
            completed = {
                **state, "pending": None, "certificates": registrations,
                "last_update": utc(datetime.now(UTC)), "last_error": None,
            }
            self.save(completed)
        except (OSError, ValueError) as exc:
            # Read the durable record even when an fsync or write reported a late failure.
            durable = self.state()
            self.recover(durable)
            detail = str(exc) if isinstance(exc, ValueError) else "Local certificate update I/O failed"
            durable["last_error"] = detail
            self.save(durable)
            raise ValueError(detail) from None
        state.update(completed)
        self.recover(state)
        for identifier in pending["changes"]:
            self.logger.info("Activated certificate %s generation %s",
                             identifier, registrations[identifier]["version"])
