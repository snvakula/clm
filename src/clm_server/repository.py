"""Transactional server publication and recovery."""

import time
from pathlib import Path
from typing import Any

from clm_common.certificate import KEYS, MAX_FILE, TRUST, json_bytes, parse_manifest, validate
from clm_common.filesystem import read_regular
from clm_common.generations import Generations
from clm_common.identity import storage_id

from .database import Database, audit, now


class Repository:
    def __init__(self, root: Path, *, trust: Path = TRUST, shared: bool = True) -> None:
        self.root = root
        self.db = Database(root / "data/clm.db")
        self.files = Generations(root / "data/certificates", shared=shared)
        self.lock = root / "run/publication.lock"
        self.trust = trust

    def certificate(self, identifier: str) -> dict[str, Any] | None:
        return self.db.one("SELECT * FROM certificates WHERE identifier=?", (identifier,))

    def ensure(self, identifier: str, algorithm: str) -> dict[str, Any]:
        with self.db.connect(write=True) as connection:
            connection.execute(
                """INSERT OR IGNORE INTO certificates
                   (identifier, api_id, algorithm, status, created) VALUES (?, ?, ?, 'pending', ?)""",
                (identifier, storage_id(identifier), algorithm, now()),
            )
        return self.certificate(identifier)

    def manifest(self, identifier: str, version: int) -> tuple[bytes, dict]:
        path = self.files.directory(identifier, version) / "manifest.json"
        data = read_regular(path, MAX_FILE)
        manifest = parse_manifest(data, identifier)
        if manifest["version"] != version:
            raise ValueError("Manifest generation does not match its directory")
        return data, manifest

    def _complete(self, entry: dict) -> None:
        identifier, version = entry["identifier"], entry["version"]
        _, manifest = self.manifest(identifier, version)
        with self.db.connect(write=True) as connection:
            connection.execute(
                """UPDATE certificates SET previous=CASE WHEN status='active' THEN generation END,
                   generation=?, serial=?, not_before=?, not_after=?, algorithm=?, account=?,
                   alias=?, status='active', last_error=NULL,
                   last_renewal=CASE WHEN ? THEN ? ELSE last_renewal END WHERE identifier=?""",
                (version, manifest["serial"], manifest["not_before"], manifest["not_after"],
                 KEYS[manifest["key_algorithm"], manifest["key_size"]], entry["account"],
                 entry["alias"], entry["renewed"], now(), identifier),
            )
            connection.execute("DELETE FROM publication WHERE identifier=?", (identifier,))
            audit(connection, "publish", identifier=identifier, generation=version)

    def _prune(self, row: dict) -> None:
        identifier = row["identifier"]
        with self.db.connect(write=True) as connection:
            connection.execute("DELETE FROM bundle_leases WHERE expires<=?", (int(time.time()),))
            leases = connection.execute(
                "SELECT version FROM bundle_leases WHERE identifier=?", (identifier,)
            ).fetchall()
        self.files.clean(identifier, {row["generation"], row["previous"], *(r[0] for r in leases)})

    def reconcile(self) -> None:
        """Call with the exclusive publication lock held."""
        for entry in self.db.all("SELECT * FROM publication"):
            identifier = entry["identifier"]
            row = self.certificate(identifier)
            current = self.files.current(identifier)
            if current not in (entry["version"], row["generation"] or None, None):
                raise ValueError("Publication journal conflicts with the current generation")
            if current != entry["version"]:
                # An interrupted publication never switched to the prepared generation.
                with self.db.connect(write=True) as connection:
                    connection.execute("DELETE FROM publication WHERE identifier=?", (identifier,))
            else:
                self._complete(entry)
        for row in self.db.all("SELECT * FROM certificates"):
            identifier = row["identifier"]
            if row["status"] == "deleted":
                if self.files.base(identifier).is_dir():
                    self.files.activate(identifier, None)
                    self.files.clean(identifier, set())
            else:
                current = self.files.current(identifier)
                expected = row["generation"] or None
                if current != expected:
                    raise ValueError(f"Repository generation differs from database for {identifier}")
                self._prune(row)

    def publish(
        self, row: dict, material: dict[str, bytes], *,
        account: str | None = None, alias: str | None = None, renewed: bool = False,
    ) -> int:
        """Validate before publication. Call with the exclusive publication lock held."""
        identifier = row["identifier"]
        version = row["generation"] + 1
        manifest = validate(identifier, version, material, self.root / "run", self.trust)
        if row["status"] == "active":
            _, previous = self.manifest(identifier, row["generation"])
            if manifest["files"] == previous["files"]:
                return row["generation"]
        self.files.clean(identifier, {
            row["generation"], row["previous"],
            *(lease["version"] for lease in self.db.all(
                "SELECT version FROM bundle_leases WHERE identifier=?", (identifier,)
            )),
        })
        self.files.stage(identifier, version, {**material, "manifest.json": json_bytes(manifest)})
        entry = dict(identifier=identifier, version=version, account=account, alias=alias,
                     renewed=int(renewed))
        with self.db.connect(write=True) as connection:
            connection.execute(
                "INSERT INTO publication VALUES (:identifier, :version, :account, :alias, :renewed)",
                entry,
            )
        self.files.activate(identifier, version)
        self._complete(entry)
        self._prune(self.certificate(identifier))
        return version
