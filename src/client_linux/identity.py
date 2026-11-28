"""Canonical certificate identifiers."""

import base64
import re

import idna


def normalise_identifier(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("Invalid DNS identifier")
    wildcard = value.startswith("*.")
    name = value[2:] if wildcard else value
    if "*" in name or not name:
        raise ValueError("Invalid wildcard or DNS identifier")
    try:
        name = (
            idna.encode(name, uts46=True, std3_rules=True).decode("ascii").lower().removesuffix(".")
        )
    except idna.IDNAError as exc:
        raise ValueError("Invalid DNS identifier") from exc
    if len(name) > 253 or "." not in name or not re.search(r"[a-z]", name.rsplit(".", 1)[1]):
        raise ValueError("Expected a DNS hostname")
    return ("*." if wildcard else "") + name


def storage_id(value: str) -> str:
    # Unpadded base64url encodes canonical identifiers without collisions.
    return base64.urlsafe_b64encode(normalise_identifier(value).encode()).decode().rstrip("=")


def storage_path(value: str) -> tuple[str, ...]:
    """Keep path components within the filesystem limit, with distinct namespaces."""
    key = storage_id(value)
    if len(key) <= 250:
        return (key,)
    return (".long", key[:200], key[200:])
