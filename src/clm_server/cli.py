"""CLM administrative command line."""

import argparse
import logging
import os
import signal
import sqlite3
import sys
from importlib.metadata import version
from pathlib import Path

from . import __version__
from .identity import normalise_identifier
from .locking import exclusive_lock, publication_lock
from .logging import configure_logging, progress
from .output import display

from .acme import LENGTHS
from .service import Administration


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(prog="clm", description="Manage certificates and client access")
    cli.add_argument("--root", type=Path, default=Path("/opt/clm"), help=argparse.SUPPRESS)
    groups = cli.add_subparsers(dest="group", required=True)
    for group, actions in (
        ("cert", ("issue", "list", "show", "renew", "rotate-key", "revoke", "delete", "import")),
        (
            "client",
            ("add", "list", "show", "grant", "deny", "rotate-token", "disable", "enable", "delete"),
        ),
    ):
        commands = groups.add_parser(group).add_subparsers(dest="action", required=True)
        for action in actions:
            command = commands.add_parser(action)
            if action != "list":
                command.add_argument(
                    "subject",
                    nargs="?" if action == "renew" else None,
                    metavar="DNS_NAME" if group == "cert" else "CLIENT",
                )
            if action in ("list", "show"):
                command.add_argument("--json", action="store_true")
            if action in ("issue", "rotate-key"):
                command.add_argument("--key", choices=LENGTHS)
            if action == "renew":
                command.add_argument("--all", action="store_true")
                command.add_argument("--force", action="store_true")
            if group == "cert" and action == "delete":
                command.add_argument("--force", action="store_true")
            if action == "import":
                command.add_argument("directory", type=Path)
            if action in ("grant", "deny"):
                command.add_argument("identifier", metavar="DNS_NAME")
    groups.add_parser("status").add_argument("--json", action="store_true")
    groups.add_parser("check")
    groups.add_parser("version")
    return cli


def check(admin: Administration) -> None:
    root = admin.root
    logger = logging.getLogger("clm")
    progress(logger, "Checking configuration and AWS account settings")
    engine = admin.engine()
    for account in engine.route.accounts:
        engine.route.credentials(account)
        engine.instance(account)
    for path, mask in (
        (root / "acme/instances", 0o077),
        (root / "config/aws", 0o077),
        (root / "config/aws/credentials", 0o077),
        (root / "data/clm.db", 0o007),
        (root / "data/certificates", 0o027),
    ):
        if path.is_symlink() or path.stat().st_mode & mask:
            raise ValueError(f"Unsafe permissions on {path}")
    progress(logger, "Checking database integrity")
    with admin.db.connect() as connection:
        if (
            connection.execute("PRAGMA quick_check").fetchone()[0] != "ok"
            or connection.execute("PRAGMA foreign_key_check").fetchone()
        ):
            raise ValueError("Database integrity check failed")
    progress(logger, "Checking the published certificate repository")
    with exclusive_lock(admin.operations), publication_lock(admin.repo.lock, write=True):
        admin.repo.reconcile()
        for row in admin.db.all("SELECT * FROM certificates WHERE status='active'"):
            identifier = row["identifier"]
            directory = admin.repo.files.directory(identifier, row["generation"])
            if (directory / "privkey.pem").stat().st_mode & 0o037:
                raise ValueError(f"Unsafe private key permissions for {identifier}")
            if not admin._usable(row):
                raise ValueError(f"Certificate validation failed for {identifier}")


def dispatch(admin: Administration, args: argparse.Namespace, cli: argparse.ArgumentParser) -> int:
    action = getattr(args, "action", args.group)
    if args.group == "cert":
        if action == "renew":
            if bool(args.subject) == args.all:
                cli.error("Specify one DNS identifier or --all")
            if args.all:
                failed = False
                rows = admin.db.all(
                    """SELECT identifier FROM certificates WHERE status='pending'
                       OR (status='active' AND account IS NOT NULL) ORDER BY identifier"""
                )
                if not rows:
                    display("No certificates configured for renewal")
                for row in rows:
                    try:
                        number = admin.obtain(row["identifier"], action, force=args.force)
                        display(f"{row['identifier']} is at generation {number}")
                    except ValueError as exc:
                        print(f"{row['identifier']}: {exc}", file=sys.stderr)
                        failed = True
                return int(failed)
        if action == "list":
            display(
                admin.db.all(
                    "SELECT * FROM certificates WHERE status<>'deleted' ORDER BY identifier"
                ),
                args.json,
            )
            return 0
        identifier = normalise_identifier(args.subject)
        if action == "show":
            row = admin.repo.certificate(identifier)
            if not row or row["status"] == "deleted":
                raise ValueError("Unknown certificate")
            display(row, args.json)
        elif action == "import":
            number = admin.import_certificate(identifier, args.directory)
            display(f"Imported {identifier} as generation {number}")
        elif action == "revoke":
            admin.revoke(identifier)
            display(f"Revoked {identifier}")
        elif action == "delete":
            admin.delete(identifier, force=args.force)
            display(f"Deleted {identifier}")
        else:
            number = admin.obtain(
                identifier,
                action,
                algorithm=getattr(args, "key", None),
                force=getattr(args, "force", False),
            )
            display(f"{identifier} generation {number}")
    elif args.group == "client":
        if action in ("list", "show"):
            columns = "id, name, enabled, created, last_authentication, last_download"
            if action == "list":
                display(admin.db.all(f"SELECT {columns} FROM clients ORDER BY name"), args.json)
            else:
                row = admin.db.one(f"SELECT {columns} FROM clients WHERE name=?", (args.subject,))
                if row is None:
                    raise ValueError("Unknown client")
                row["certificates"] = [
                    grant["identifier"]
                    for grant in admin.db.all(
                        "SELECT identifier FROM grants WHERE client_id=? ORDER BY identifier",
                        (row["id"],),
                    )
                ]
                display(row, args.json)
        else:
            identifier = (
                normalise_identifier(args.identifier) if action in ("grant", "deny") else None
            )
            token = admin.client_change(args.subject, action, identifier)
            if token:
                # Keep the token as the sole stdout value for provisioning scripts.
                print(token)
            elif action == "grant":
                display(f"Granted {args.subject} access to {identifier}")
            elif action == "deny":
                display(f"Removed {args.subject}'s access to {identifier}")
            else:
                verbs = {"disable": "Disabled", "enable": "Enabled", "delete": "Deleted"}
                display(f"{verbs[action]} client {args.subject}")
    elif action == "status":
        with publication_lock(admin.repo.lock, write=False):
            certificates = admin.db.all(
                """SELECT identifier, generation, not_after, last_attempt, last_renewal, last_error,
                          status FROM certificates WHERE status<>'deleted' ORDER BY identifier"""
            )
            inconsistent = []
            for row in certificates:
                try:
                    if admin.repo.files.current(row["identifier"]) != (row["generation"] or None):
                        inconsistent.append(row["identifier"])
                except (OSError, ValueError):
                    inconsistent.append(row["identifier"])
            display(
                {"certificates": certificates, "repository_inconsistencies": inconsistent},
                args.json,
            )
    elif action == "check":
        check(admin)
        print("Configuration, database and certificate repository checks passed")
    return 0


def _terminate(signum: int, frame: object) -> None:
    raise OSError("CLM operation interrupted")


def main() -> int:
    os.umask(0o077)
    signal.signal(signal.SIGTERM, _terminate)
    cli = parser()
    args = cli.parse_args()
    if args.group == "version":
        print(__version__ or version("clm"))
        return 0
    admin = Administration(args.root.resolve())
    logger = configure_logging(admin.root / "logs", "clm", terminal=True)
    try:
        result = dispatch(admin, args, cli)
    except (OSError, ValueError, sqlite3.Error) as exc:
        message = (
            str(exc) if isinstance(exc, ValueError) else "CLM could not read or update local state"
        )
        logger.error("%s", message)
        print(message, file=sys.stderr)
        return 1
    logger.info(
        "%s %s completed with exit status %s", args.group, getattr(args, "action", ""), result
    )
    return result
