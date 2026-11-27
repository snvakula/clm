"""Public CNAME discovery and per-account static AWS credentials."""

import configparser
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from clm_common.config import load_config
from clm_common.filesystem import read_regular
from clm_common.identity import normalise_identifier

PREFIX = "_acme-challenge."
LABEL = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}")
ZONE = re.compile(r"Z[A-Z0-9]{1,31}")


def dns_name(value: str) -> str:
    name = value.lower().removesuffix(".")
    if (
        len(name) > 253
        or "." not in name
        or not all(re.fullmatch(r"[a-z0-9_-]{1,63}", label) for label in name.split("."))
    ):
        raise ValueError("Invalid public DNS record name")
    return name


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def _public_records(name: str, kind: str, number: int) -> list[str]:
    url = "https://dns.google/resolve?" + urllib.parse.urlencode({"name": name, "type": kind})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(url, timeout=10) as response:
            payload = json.loads(response.read(65537))
    except (OSError, ValueError):
        raise ValueError(f"Public DNS lookup failed for {name}") from None
    if payload.get("Status") not in (0, 3):
        raise ValueError(f"Public DNS lookup failed for {name}")
    return [
        dns_name(item["data"]) for item in payload.get("Answer", [])
        if item["type"] == number and dns_name(item["name"]) == name
    ]


def public_cnames(name: str) -> list[str]:
    return _public_records(name, "CNAME", 5)


def public_nameservers(name: str) -> list[str]:
    return _public_records(name, "NS", 2)


@dataclass(frozen=True)
class Target:
    account: str
    alias: str
    zone: str

    @property
    def record(self) -> str:
        return PREFIX + self.alias


class Route53:
    def __init__(self, config: Path) -> None:
        value = load_config(config / "route53.yaml")
        if set(value) != {"accounts", "challenge_aliases"}:
            raise ValueError("Expected Route 53 accounts and challenge aliases")
        accounts, aliases = value["accounts"], value["challenge_aliases"]
        if not isinstance(accounts, dict) or not isinstance(aliases, dict):
            raise ValueError("Invalid Route 53 accounts or challenge aliases")
        self.accounts = {}
        self.aliases = {}
        self.credentials_path = config / "aws/credentials"
        for name, entry in accounts.items():
            if (
                not LABEL.fullmatch(name)
                or not isinstance(entry, dict)
                or set(entry) != {"credentials_profile"}
                or not isinstance(entry["credentials_profile"], str)
                or not LABEL.fullmatch(entry["credentials_profile"])
            ):
                raise ValueError("Invalid AWS account configuration")
            self.accounts[name] = entry["credentials_profile"]
        if len(set(self.accounts.values())) != len(self.accounts):
            raise ValueError("Each AWS account must have its own credentials profile")
        for name, entry in aliases.items():
            alias = normalise_identifier(name)
            if (
                alias.startswith("*.")
                or len(PREFIX + alias) > 253
                or alias in self.aliases
                or not isinstance(entry, dict)
                or set(entry) != {"account", "hosted_zone_id"}
                or not isinstance(entry["account"], str)
                or entry["account"] not in self.accounts
                or not isinstance(entry["hosted_zone_id"], str)
                or not ZONE.fullmatch(entry["hosted_zone_id"])
            ):
                raise ValueError("Invalid or ambiguous challenge alias configuration")
            self.aliases[alias] = Target(entry["account"], alias, entry["hosted_zone_id"])

    def discover(
        self, identifier: str, lookup: Callable[[str], list[str]] = public_cnames,
    ) -> Target:
        first = dns_name(PREFIX + identifier.removeprefix("*."))
        name = first
        seen = set()
        for _ in range(17):
            if name in seen:
                raise ValueError(f"CNAME loop for {first}")
            seen.add(name)
            alias = self.aliases.get(name.removeprefix(PREFIX)) if name.startswith(PREFIX) else None
            records = set(lookup(name))
            if len(records) > 1 or (alias and records):
                raise ValueError(f"Ambiguous public challenge endpoint for {first}")
            if not records:
                if alias and name != first:
                    return alias
                raise ValueError(f"Configure a public CNAME at {first} to a known challenge alias")
            name = dns_name(records.pop())
        raise ValueError(f"CNAME chain exceeds the limit for {first}")

    def credentials(self, account: str) -> dict[str, str]:
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(read_regular(self.credentials_path, 64 * 1024, private=True).decode())
            entry = parser[self.accounts[account]]
            result = {
                "AWS_ACCESS_KEY_ID": entry["aws_access_key_id"],
                "AWS_SECRET_ACCESS_KEY": entry["aws_secret_access_key"],
            }
            if entry.get("aws_session_token"):
                result["AWS_SESSION_TOKEN"] = entry["aws_session_token"]
            if any(not re.fullmatch(r"[A-Za-z0-9/+=_-]+", field) for field in result.values()):
                raise ValueError
        except (KeyError, configparser.Error, OSError, ValueError):
            raise ValueError(f"Invalid or missing credentials for AWS account {account}") from None
        return result
