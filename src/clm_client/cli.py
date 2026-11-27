"""CLM client command line."""

import argparse
import getpass
import json
import os
import sys
from importlib.metadata import version
from pathlib import Path

from clm_common.filesystem import read_regular
from clm_common.identity import normalise_identifier
from clm_common.locking import exclusive_lock
from clm_common.logging import configure_logging

from .application import Client


def arguments() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clm-client", description="Install and update certificates")
    parser.add_argument("--root", type=Path, default=Path("/opt/clm-client"), help=argparse.SUPPRESS)
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


def display(value, machine: bool = False) -> None:
    if machine:
        print(json.dumps(value, indent=2, sort_keys=True))
    elif isinstance(value, list):
        for item in value:
            display(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            print(f"{key}: {item}")
    else:
        print(value)


def dispatch(args: argparse.Namespace, client: Client) -> None:
    action = args.action
    state = client.state()
    if action == "status":
        display({
            **state, "pending": bool(state["pending"]),
            "repository_inconsistencies": [
                identifier for identifier, row in state["certificates"].items()
                if client.files.current(identifier) != row["version"]
            ],
        }, args.json)
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
        display(client.remote().available(), args.json)
    elif action == "install":
        client.apply(state, [identifier], service_names=args.service)
        info = client.inspect(state, identifier)
        display({"certificate_path": info["certificate_path"], "private_key_path": info["private_key_path"]})
    elif action == "update":
        if identifier and identifier not in state["certificates"]:
            raise ValueError("Certificate is not installed")
        client.apply(state, [identifier] if identifier else sorted(state["certificates"]))
    elif action == "list":
        display([client.inspect(state, item) for item in sorted(state["certificates"])], args.json)
    elif action == "show":
        display(client.inspect(state, identifier), args.json)
    elif action == "remove":
        client.remove(state, identifier, args.force)


def main() -> int:
    os.umask(0o077)
    args = arguments().parse_args()
    if args.action == "version":
        print(version("clm"))
        return 0
    root = args.root.resolve()
    logger = configure_logging(root / "logs", "clm-client")
    client = Client(root, logger)
    try:
        with exclusive_lock(root / "run/update.lock"):
            dispatch(args, client)
    except (OSError, ValueError, EOFError) as exc:
        message = str(exc) if type(exc) is ValueError else "CLM client could not read or update local state"
        logger.error("%s", message)
        print(message, file=sys.stderr)
        return 1
    return 0
