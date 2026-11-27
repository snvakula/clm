"""CLM client command line."""

import argparse
import os
import sys
from importlib.metadata import version
from pathlib import Path

from . import __version__
from .locking import exclusive_lock
from .logging import configure_logging
from .output import display

from .application import Client


def arguments() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clm-client", description="Install and update certificates"
    )
    parser.add_argument(
        "--root", type=Path, default=Path("/opt/clm-client"), help=argparse.SUPPRESS
    )
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("update", help="Fetch all certificates assigned by the CLM server")
    commands.add_parser("status", help="Show local certificates and the last update").add_argument(
        "--json",
        action="store_true",
    )
    commands.add_parser("version", help="Show the client version")
    return parser


def dispatch(args: argparse.Namespace, client: Client) -> None:
    action = args.action
    state = client.state()
    if action == "status":
        inconsistent = []
        certificates = []
        for identifier, row in sorted(state["certificates"].items()):
            try:
                if client.files.current(identifier) != row["version"]:
                    raise ValueError("Unexpected active generation")
                info = client.inspect(state, identifier)
                certificates.append(
                    {
                        "identifier": identifier,
                        "version": row["version"],
                        "not_after": info["not_after"],
                        "certificate_path": info["certificate_path"],
                        "private_key_path": info["private_key_path"],
                    }
                )
            except (OSError, ValueError):
                inconsistent.append(identifier)
        display(
            {
                "certificates": certificates,
                "pending": bool(state["pending"]),
                "last_update": state["last_update"],
                "last_error": state["last_error"],
                "repository_inconsistencies": inconsistent,
            },
            args.json,
        )
        return
    assigned, updated = client.update(state)
    if assigned:
        display(f"Checked {assigned} assigned certificates. Updated {updated}.")
    else:
        display("The CLM server has assigned no certificates to this client")


def main() -> int:
    os.umask(0o077)
    args = arguments().parse_args()
    if args.action == "version":
        print(__version__ or version("clm"))
        return 0
    root = args.root.resolve()
    logger = configure_logging(root / "logs", "clm-client", terminal=True)
    client = Client(root, logger)
    try:
        with exclusive_lock(root / "run/update.lock"):
            dispatch(args, client)
    except (OSError, ValueError, EOFError) as exc:
        message = (
            str(exc)
            if type(exc) is ValueError
            else "CLM client could not read or update local state"
        )
        logger.error("%s", message)
        print(message, file=sys.stderr)
        return 1
    return 0
