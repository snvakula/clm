"""Read-only distribution endpoints with per-request authentication and authorisation."""

import argparse
import errno
import hmac
import ipaddress
import logging
import os
import re
import sqlite3
import time
from collections import OrderedDict
from pathlib import Path
from threading import Lock

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response

from .certificate import checksums, make_bundle, read_material
from .locking import publication_lock
from .logging import configure_logging

from .database import audit, now
from .repository import Repository
from .service import token_hash

LEASE_SECONDS = 300
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


class Attempts:
    def __init__(self) -> None:
        self.entries: OrderedDict[str, tuple[float, int]] = OrderedDict()
        self.lock = Lock()

    def limited(self, source: str) -> bool:
        with self.lock:
            deadline, count = self.entries.get(source, (0.0, 0))
            return deadline > time.monotonic() and count >= 10

    def fail(self, source: str) -> None:
        with self.lock:
            current = time.monotonic()
            deadline, count = self.entries.get(source, (0.0, 0))
            self.entries[source] = (
                (deadline, count + 1)
                if deadline > current
                else (
                    current + 60,
                    1,
                )
            )
            self.entries.move_to_end(source)
            if len(self.entries) > 4096:
                self.entries.popitem(last=False)


def create_app(root: Path) -> FastAPI:
    repo = Repository(root)
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    attempts = Attempts()
    logger = logging.getLogger("clmd")

    @app.middleware("http")
    async def no_store(request: Request, call_next) -> Response:
        try:
            response = await call_next(request)
        except (OSError, sqlite3.Error, ValueError) as error:
            route = request.scope.get("route")
            reason = type(error).__name__
            if isinstance(error, OSError):
                reason = errno.errorcode.get(error.errno, reason)
            elif isinstance(error, sqlite3.Error):
                reason = getattr(error, "sqlite_errorname", reason)
            logger.error(
                "Distribution request failed for %s (%s)", getattr(route, "path", "-"), reason
            )
            response = Response(status_code=503)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def authenticate(connection: sqlite3.Connection, request: Request) -> tuple:
        try:
            source = str(ipaddress.ip_address(request.headers.get("x-clm-source", "")))
        except ValueError:
            source = "local"
        if attempts.limited(source):
            raise HTTPException(429, "Too many authentication attempts")
        authorisation = request.headers.getlist("authorization")
        scheme, _, token = (
            authorisation[0].partition(" ") if len(authorisation) == 1 else ("", "", "")
        )
        digest = (
            token_hash(token)
            if scheme.lower() == "bearer" and TOKEN.fullmatch(token)
            else bytes(32)
        )
        client = connection.execute(
            "SELECT * FROM clients WHERE token_hash=?",
            (digest,),
        ).fetchone()
        if (
            not client
            or not client["enabled"]
            or not hmac.compare_digest(client["token_hash"], digest)
        ):
            attempts.fail(source)
            logger.warning("Authentication failed from %s", source)
            audit(connection, "authenticate", "unknown", result="failure", source=source)
            connection.commit()
            raise HTTPException(
                401,
                "Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        connection.execute(
            "UPDATE clients SET last_authentication=? WHERE id=?",
            (now(), client["id"]),
        )
        return client, source

    def accessible(connection: sqlite3.Connection, client_id: int, api_id: str) -> sqlite3.Row:
        row = connection.execute(
            """SELECT c.* FROM certificates c JOIN grants g ON c.identifier=g.identifier
               WHERE g.client_id=? AND c.api_id=? AND c.status='active' AND c.not_after>?""",
            (client_id, api_id, now()),
        ).fetchone()
        if row is None:
            connection.commit()
            raise HTTPException(404, "Certificate unavailable")
        return row

    @app.get("/v1/health")
    def health() -> dict:
        repo.db.one("SELECT 1")
        return {"status": "ok"}

    @app.get("/v1/client")
    def client_info(request: Request) -> dict:
        with repo.db.connect(write=True) as connection:
            client, _ = authenticate(connection, request)
            return {"name": client["name"]}

    @app.get("/v1/certificates")
    def certificates(request: Request) -> dict:
        with repo.db.connect(write=True) as connection:
            client, _ = authenticate(connection, request)
            rows = connection.execute(
                """SELECT c.identifier AS certificate_id, c.api_id AS id,
                          c.generation AS version, c.not_after
                   FROM certificates c JOIN grants g ON c.identifier=g.identifier
                   WHERE g.client_id=? AND c.status='active' AND c.not_after>?
                   ORDER BY c.identifier""",
                (client["id"], now()),
            )
            return {"certificates": [dict(row) for row in rows]}

    @app.get("/v1/certificates/{api_id}/manifest")
    def manifest(api_id: str, request: Request) -> Response:
        with publication_lock(repo.lock, write=False), repo.db.connect(write=True) as connection:
            client, _ = authenticate(connection, request)
            row = accessible(connection, client["id"], api_id)
            data, _ = repo.manifest(row["identifier"], row["generation"])
            stamp = int(time.time())
            connection.execute("DELETE FROM bundle_leases WHERE expires<=?", (stamp,))
            connection.execute(
                """INSERT OR REPLACE INTO bundle_leases VALUES (?, ?, ?, ?, ?)""",
                (
                    client["id"],
                    row["identifier"],
                    row["generation"],
                    client["token_hash"],
                    stamp + LEASE_SECONDS,
                ),
            )
            return Response(content=data, media_type="application/json")

    @app.get("/v1/certificates/{api_id}/versions/{version}/bundle")
    def bundle(api_id: str, version: str, request: Request) -> Response:
        with publication_lock(repo.lock, write=False), repo.db.connect(write=True) as connection:
            client, source = authenticate(connection, request)
            row = accessible(connection, client["id"], api_id)
            if not re.fullmatch(r"[1-9][0-9]{0,18}", version) or int(version) >= 2**63:
                connection.commit()
                raise HTTPException(404, "Certificate unavailable")
            number = int(version)
            if (
                number != row["generation"]
                and not connection.execute(
                    """SELECT 1 FROM bundle_leases WHERE client_id=? AND identifier=? AND version=?
                   AND token_hash=? AND expires>?""",
                    (
                        client["id"],
                        row["identifier"],
                        number,
                        client["token_hash"],
                        int(time.time()),
                    ),
                ).fetchone()
            ):
                connection.commit()
                raise HTTPException(404, "Certificate unavailable")
            data, metadata = repo.manifest(row["identifier"], number)
            if metadata["not_after"] <= now():
                connection.commit()
                raise HTTPException(404, "Certificate unavailable")
            material = read_material(repo.files.directory(row["identifier"], number))
            if metadata["files"] != checksums(material):
                raise ValueError("Corrupt certificate generation")
            payload = make_bundle(material, data)
            connection.execute(
                "UPDATE clients SET last_download=? WHERE id=?",
                (now(), client["id"]),
            )
            audit(
                connection,
                "download",
                client["name"],
                identifier=row["identifier"],
                generation=number,
                source=source,
            )
            logger.info(
                "Distributed %s generation %s to %s", row["identifier"], number, client["name"]
            )
            return Response(content=payload, media_type="application/zip")

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="CLM certificate distribution service")
    parser.add_argument("--root", type=Path, default=Path("/opt/clm"))
    parser.add_argument("--fd", type=int, default=3, help="Inherited Unix listening socket")
    args = parser.parse_args()
    os.umask(0o077)
    root = args.root.resolve()
    configure_logging(root / "logs", "clmd").info("Starting certificate distribution")
    uvicorn.run(create_app(root), fd=args.fd, proxy_headers=False, access_log=False)
