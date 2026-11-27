"""Canonical certificate identifiers."""
import base64
import re

import idna


def normalise_identifier(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("Invalid DNS identifier")
    wildcard = value.startswith("*.")
    name = value[2:] if wildcard else value
    if name.endswith("."):
        name = name[:-1]
    if "*" in name or not name:
        raise ValueError("Invalid wildcard or DNS identifier")
    try:
        name = idna.encode(name, uts46=True, std3_rules=True).decode("ascii").lower()
    except idna.IDNAError as exc:
        raise ValueError("Invalid DNS identifier") from exc
    if len(name) > 253 or "." not in name or not re.search(r"[a-z]", name.rsplit(".", 1)[1]):
        raise ValueError("Expected a DNS hostname")
    return ("*." if wildcard else "") + name


def storage_id(value: str) -> str:
    # Hex encoding is reversible and collision-free; split long identifiers into
    # path components at repository level rather than using this as a filename.
    return base64.urlsafe_b64encode(normalise_identifier(value).encode()).decode().rstrip("=")
