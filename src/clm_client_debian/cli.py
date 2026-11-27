"""CLM client command line."""

import argparse
import getpass
import os
import sys
from importlib.metadata import version
from pathlib import Path

from . import __version__
from .filesystem import read_regular
from .identity import normalise_identifier
from .locking import exclusive_lock
from .logging import configure_logging, progress
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
    init = commands.add_parser("init")
    init.add_argument("server")
    sources = init.add_mutually_exclusive_group()
    sources.add_argument("--credentials-file", type=Path)
    sources.add_argument("--token-stdin", action="store_true")
    for name in ("available", "install", "list", "show", "update", "remove", "status", "version"):
        command = commands.add_parser(name)
        if name in ("install", "show", "update", "remove"):
            command.add_argument("identifier", nargs="?" if name == "update" else None)
        if name in ("available", "list", "show", "status"):
            command.add_argument("--json", action="store_true")
        if name == "install":
            command.add_argument("--service", action="append")
        if name == "remove":
            command.add_argument("--force", action="store_true")
    return parser


def dispatch(args: argparse.Namespace, client: Client) -> None:
    action = args.action
    state = client.state()
    if action == "status":
        inconsistent = []
        certificates = []
        for identifier, row in state["certificates"].items():
            try:
                if client.files.current(identifier) != row["version"]:
                    raise ValueError("Unexpected active generation")
                info = client.inspect(state, identifier)
                certificates.append(
                    {
                        "identifier": identifier,
                        "version": row["version"],
                        "not_after": info["not_after"],
                        "services": row["services"],
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
    client.recover(state)
    value = getattr(args, "identifier", None)
    identifier = normalise_identifier(value) if value else None
    if action == "init":
        if args.credentials_file is not None:
            token = read_regular(args.credentials_file, 4096, private=True).decode().strip()
        elif args.token_stdin:
            token = sys.stdin.readline(4097).strip()
        else:
            with open("/dev/tty", "r+") as terminal:
                token = getpass.getpass("CLM access token: ", stream=terminal)
        display(f"Registered with the CLM server as {client.initialise(args.server, token)}")
    elif action == "available":
        if not args.json:
            progress(client.logger, "Fetching the list of certificates from CLM")
        display(client.remote().available(), args.json)
    elif action == "install":
        client.apply(state, [identifier], service_names=args.service)
        info = client.inspect(state, identifier)
        display(
            {
                "certificate_path": info["certificate_path"],
                "private_key_path": info["private_key_path"],
            }
        )
    elif action == "update":
        if identifier and identifier not in state["certificates"]:
            raise ValueError("Certificate is not installed")
        identifiers = [identifier] if identifier else sorted(state["certificates"])
        if identifiers:
            client.apply(state, identifiers)
            display("Certificate update check complete")
        else:
            display("No certificates installed")
    elif action == "list":
        display([client.inspect(state, item) for item in sorted(state["certificates"])], args.json)
    elif action == "show":
        display(client.inspect(state, identifier), args.json)
    elif action == "remove":
        client.remove(state, identifier, args.force)
        display(f"Removed {identifier}")


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
