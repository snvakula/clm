"""Administrative certificate lifecycle and access management."""

import hashlib
import logging
import re
import secrets

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from clm_common.certificate import KEYS, MAX_FILE, checksums, json_bytes, read_material, validate
from clm_common.config import load_config
from clm_common.filesystem import read_regular, sync_directory, write_atomic
from clm_common.identity import storage_id
from clm_common.locking import exclusive_lock, publication_lock

from .acme import LENGTHS, Acme
from .database import audit, now
from .migration import Migration
from .repository import Repository
from .route53 import Route53

CLIENT = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,127}")


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def account_email(path: Path) -> str:
    settings = load_config(path)
    email = settings.get("account_email")
    if (
        set(settings) != {"account_email"}
        or not isinstance(email, str)
        or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)
    ):
        raise ValueError("Configure a valid ACME account email in clm.yaml")
    return email


class Administration:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = Repository(root)
        self.db = self.repo.db
        self.operations = root / "run/operations.lock"
        self.operation_descriptor: int | None = None

    @contextmanager
    def operation_lock(self) -> Iterator[None]:
        with exclusive_lock(self.operations) as descriptor:
            self.operation_descriptor = descriptor
            try:
                yield
            finally:
                self.operation_descriptor = None

    def engine(self) -> Acme:
        email = account_email(self.root / "config/clm.yaml")
        engine = Acme(self.root, Route53(self.root / "config"), email)
        engine.lock_descriptor = self.operation_descriptor
        if not engine.ready():
            raise ValueError("The reviewed ACME engine is missing or incomplete")
        return engine

    def event(
        self,
        operation: str,
        *,
        identifier: str | None = None,
        detail: str | None = None,
        actor: str = "administrator",
    ) -> None:
        logger = logging.getLogger("clm")
        if detail:
            logger.error("%s failed for %s: %s", operation, identifier, detail)
        else:
            logger.info("%s completed for %s", operation, identifier)
        with self.db.connect(write=True) as connection:
            audit(
                connection,
                operation,
                actor,
                identifier=identifier,
                detail=detail,
                result="failure" if detail else "success",
            )
            if identifier is not None:
                connection.execute(
                    "UPDATE certificates SET last_error=? WHERE identifier=?",
                    (detail, identifier),
                )

    def _usable(self, row: dict) -> bool:
        if row["status"] != "active" or row["not_after"] <= now():
            return False
        identifier, version = row["identifier"], row["generation"]
        try:
            _, manifest = self.repo.manifest(identifier, version)
            return self.repo.files.current(identifier) == version and manifest == validate(
                identifier,
                version,
                read_material(self.repo.files.directory(identifier, version)),
                self.root / "run",
                self.repo.trust,
            )
        except (OSError, ValueError):
            return False

    def _candidate(self, account: str, identifier: str) -> Path:
        key = hashlib.sha256(storage_id(identifier).encode()).hexdigest()
        return self.root / "acme/instances" / account / "config" / f"candidate-{key}.json"

    def _recover_migrations(self, intent: dict | None = None) -> None:
        instances = self.root / "acme/instances"
        if not instances.is_dir():
            return
        for instance in instances.iterdir():
            if instance.is_symlink() or not instance.is_dir():
                continue
            migration = Migration(instance)
            if not migration.path.exists():
                if any(instance.glob(".migration-*")):
                    migration.finish()
                continue
            saved = migration.intent()
            row = self.repo.certificate(saved["id"])
            if (
                row
                and row["generation"] > saved["generation"]
                and (row["account"], row["alias"], row["zone"])
                == (saved["account"], saved["alias"], saved["zone"])
            ):
                migration.finish()
                continue
            candidate = self._candidate(instance.name, saved["id"])
            if (
                intent is not None
                and saved == intent
                and candidate.exists()
                and read_regular(candidate, 4096, private=True) == json_bytes(intent)
            ):
                continue
            migration.restore()

    def _obtain(
        self,
        identifier: str,
        operation: str,
        *,
        algorithm: str | None,
        force: bool,
    ) -> int:
        with publication_lock(self.repo.lock, write=True):
            self.repo.reconcile(repairing=identifier)
            row = self.repo.certificate(identifier)
            key = algorithm or (row["algorithm"] if row else "ec-256")
            if key not in LENGTHS:
                raise ValueError("Unsupported certificate key algorithm")
            if row is None and operation != "issue":
                raise ValueError("Unknown certificate")
            if row and row["status"] in ("revoking", "revoked", "deleted") and operation == "renew":
                raise ValueError("Certificate requires explicit issuance")
            if row and key != row["algorithm"] and operation != "rotate-key":
                raise ValueError("Use rotate-key --key to change the key algorithm")
            usable = bool(row and self._usable(row))
            if operation == "issue" and usable:
                return row["generation"]
            if row is None:
                row = self.repo.ensure(identifier, key)
        with self.db.connect(write=True) as connection:
            connection.execute(
                "UPDATE certificates SET last_attempt=? WHERE identifier=?",
                (now(), identifier),
            )
        engine = self.engine()
        target = engine.route.discover(identifier)
        migrating = (row["account"], row["alias"], row["zone"]) != (
            target.account,
            target.alias,
            target.zone,
        )
        issue = migrating or not row["account"] or key != row["algorithm"]
        if row["status"] in ("revoked", "deleted", "revoking"):
            issue = True
        forced = force or migrating or not usable or operation == "rotate-key"
        marker = self._candidate(target.account, identifier)
        instance = engine.instance(target.account)
        intent = {
            "id": identifier,
            "generation": row["generation"],
            "key": key,
            "account": target.account,
            "alias": target.alias,
            "zone": target.zone,
        }
        engine.recover_dns()
        self._recover_migrations(intent)
        migration = Migration(instance)
        if migrating and row["account"] == target.account:
            migration.begin(intent)
        record = json_bytes(intent)
        try:
            # Reuse a successful ACME result after an interrupted publication.
            resumed = marker.exists() and read_regular(marker, 4096, private=True) == record
            if not resumed:
                changed = engine.obtain(target, identifier, key, issue=issue, force=forced)
                if changed:
                    write_atomic(marker, record)
                elif not usable:
                    raise ValueError("ACME skipped a certificate that requires repair")
            material = engine.export(target, identifier, key)
            candidate = validate(
                identifier,
                row["generation"] + 1,
                material,
                self.root / "run",
                self.repo.trust,
            )
            if KEYS[candidate["key_algorithm"], candidate["key_size"]] != key:
                raise ValueError("ACME returned an unexpected key algorithm")
            if row["status"] == "active":
                try:
                    _, previous = self.repo.manifest(identifier, row["generation"])
                except (OSError, ValueError):
                    previous = None
                if previous and previous["files"] == candidate["files"]:
                    if operation == "rotate-key" or migrating or not usable:
                        raise ValueError("ACME did not produce new certificate material")
                else:
                    live = self.repo.files.directory(identifier, row["generation"])
                    try:
                        old_leaf = x509.load_pem_x509_certificate(
                            read_regular(live / "cert.pem", MAX_FILE),
                        )
                    except (OSError, ValueError):
                        old_leaf = None
                    new_leaf = x509.load_pem_x509_certificate(material["cert.pem"])
                    if old_leaf and old_leaf.public_key().public_bytes(
                        Encoding.DER,
                        PublicFormat.SubjectPublicKeyInfo,
                    ) == new_leaf.public_key().public_bytes(
                        Encoding.DER,
                        PublicFormat.SubjectPublicKeyInfo,
                    ):
                        raise ValueError("ACME reused the previous private key")
        except (OSError, ValueError):
            marker.unlink(missing_ok=True)
            if migration.path.exists():
                migration.restore()
            raise
        with publication_lock(self.repo.lock, write=True):
            version = self.repo.publish(
                row,
                material,
                account=target.account,
                alias=target.alias,
                zone=target.zone,
                renewed=bool(row["generation"]),
            )
        if migration.path.exists():
            migration.finish()
        marker.unlink(missing_ok=True)
        sync_directory(marker.parent)
        return version

    def obtain(
        self,
        identifier: str,
        operation: str,
        *,
        algorithm: str | None = None,
        force: bool = False,
    ) -> int:
        with self.operation_lock():
            return self._recorded_obtain(identifier, operation, algorithm, force)

    def _recorded_obtain(
        self,
        identifier: str,
        operation: str,
        algorithm: str | None = None,
        force: bool = False,
    ) -> int:
        try:
            version = self._obtain(identifier, operation, algorithm=algorithm, force=force)
        except (OSError, ValueError) as exc:
            message = (
                str(exc) if isinstance(exc, ValueError) else "Certificate operation I/O failed"
            )
            self.event(operation, identifier=identifier, detail=message)
            raise ValueError(message) from None
        self.event(operation, identifier=identifier)
        return version

    def import_certificate(self, identifier: str, directory: Path) -> int:
        with self.operation_lock(), publication_lock(self.repo.lock, write=True):
            self.repo.reconcile()
            material = read_material(directory)
            manifest = validate(identifier, 1, material, self.root / "run", self.repo.trust)
            key = KEYS[manifest["key_algorithm"], manifest["key_size"]]
            row = self.repo.ensure(identifier, key)
            if row["account"]:
                raise ValueError("An ACME-managed certificate already exists")
            if row["generation"] and row["algorithm"] != key:
                raise ValueError("Existing certificate uses a different key algorithm")
            version = self.repo.publish(row, material)
            self.event("import", identifier=identifier)
            return version

    def revoke(self, identifier: str) -> None:
        with self.operation_lock():
            with publication_lock(self.repo.lock, write=True):
                self.repo.reconcile()
                row = self.repo.certificate(identifier)
            if not row or not row["account"] or row["status"] not in ("active", "revoking"):
                raise ValueError("Certificate has no revocable ACME-managed generation")
            engine = self.engine()
            engine.recover_dns()
            self._recover_migrations()
            target = engine.route.aliases.get(row["alias"])
            if target is None or (target.account, target.zone) != (row["account"], row["zone"]):
                raise ValueError("Restore the original account mapping before revocation")
            material = engine.export(target, identifier, row["algorithm"])
            _, manifest = self.repo.manifest(identifier, row["generation"])
            if manifest["files"] != checksums(material):
                raise ValueError("ACME certificate differs from the published generation")
            with self.db.connect(write=True) as connection:
                connection.execute(
                    "UPDATE certificates SET status='revoking' WHERE identifier=?",
                    (identifier,),
                )
            try:
                engine.revoke(target, identifier, row["algorithm"])
            except ValueError as exc:
                self.event("revoke", identifier=identifier, detail=str(exc))
                raise
            with self.db.connect(write=True) as connection:
                connection.execute(
                    "UPDATE certificates SET status='revoked', last_error=NULL WHERE identifier=?",
                    (identifier,),
                )
                audit(connection, "revoke", identifier=identifier, generation=row["generation"])

    def delete(self, identifier: str, *, force: bool) -> None:
        with self.operation_lock(), publication_lock(self.repo.lock, write=True):
            self.repo.reconcile()
            row = self.repo.certificate(identifier)
            if row is None or row["status"] == "deleted":
                raise ValueError("Unknown certificate")
            with self.db.connect(write=True) as connection:
                assigned = connection.execute(
                    """SELECT 1 FROM grants JOIN clients ON clients.id=grants.client_id
                       WHERE grants.identifier=? AND clients.enabled=1""",
                    (identifier,),
                ).fetchone()
                if assigned and not force:
                    raise ValueError("Certificate has active client assignments; use --force")
                connection.execute("DELETE FROM grants WHERE identifier=?", (identifier,))
                connection.execute("DELETE FROM bundle_leases WHERE identifier=?", (identifier,))
                connection.execute(
                    "UPDATE certificates SET status='deleted' WHERE identifier=?",
                    (identifier,),
                )
                audit(connection, "delete", identifier=identifier)
            self.repo.reconcile()

    @staticmethod
    def _client(connection: Any, name: str) -> Any:
        if not CLIENT.fullmatch(name):
            raise ValueError("Invalid client name")
        client = connection.execute("SELECT * FROM clients WHERE name=?", (name,)).fetchone()
        if client is None:
            raise ValueError("Unknown client")
        return client

    def client_change(self, name: str, operation: str, identifier: str | None = None) -> str | None:
        if not CLIENT.fullmatch(name):
            raise ValueError("Invalid client name")
        if operation == "grant":
            with self.operation_lock():
                with self.db.connect() as connection:
                    client_id = self._client(connection, name)["id"]
                self._recorded_obtain(identifier, "issue")
                with self.db.connect(write=True) as connection:
                    client = self._client(connection, name)
                    if client["id"] != client_id:
                        raise ValueError("Client was replaced while preparing the certificate")
                    connection.execute(
                        "INSERT OR IGNORE INTO grants VALUES (?, ?, ?)",
                        (client["id"], identifier, now()),
                    )
                    audit(connection, operation, f"administrator for {name}", identifier=identifier)
            return None
        token = None
        with self.db.connect(write=True) as connection:
            if operation == "add":
                if connection.execute("SELECT 1 FROM clients WHERE name=?", (name,)).fetchone():
                    raise ValueError("Client already exists")
                token = secrets.token_urlsafe(32)
                connection.execute(
                    "INSERT INTO clients (name, token_hash, created) VALUES (?, ?, ?)",
                    (name, token_hash(token), now()),
                )
            else:
                client_id = self._client(connection, name)["id"]
                if operation == "rotate-token":
                    token = secrets.token_urlsafe(32)
                    connection.execute(
                        "UPDATE clients SET token_hash=? WHERE id=?",
                        (token_hash(token), client_id),
                    )
                    connection.execute("DELETE FROM bundle_leases WHERE client_id=?", (client_id,))
                elif operation in ("enable", "disable"):
                    connection.execute(
                        "UPDATE clients SET enabled=? WHERE id=?",
                        (operation == "enable", client_id),
                    )
                    if operation == "disable":
                        connection.execute(
                            "DELETE FROM bundle_leases WHERE client_id=?", (client_id,)
                        )
                elif operation == "deny":
                    connection.execute(
                        "DELETE FROM grants WHERE client_id=? AND identifier=?",
                        (client_id, identifier),
                    )
                    connection.execute(
                        "DELETE FROM bundle_leases WHERE client_id=? AND identifier=?",
                        (client_id, identifier),
                    )
                elif operation == "delete":
                    connection.execute("DELETE FROM clients WHERE id=?", (client_id,))
                else:
                    raise ValueError("Invalid client operation")
            audit(connection, operation, f"administrator for {name}", identifier=identifier)
        return token
