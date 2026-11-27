"""Validate downloaded certificates, manifests and bundles."""

import hashlib
import io
import json
import re
import subprocess
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from .filesystem import read_regular
from .identity import normalise_identifier

PEMS = ("cert.pem", "chain.pem", "fullchain.pem", "privkey.pem")
FILES = (*PEMS, "manifest.json")
MAX_FILE = 64 * 1024
MAX_BUNDLE = 5 * MAX_FILE + 4096
TRUST = Path("/etc/ssl/certs/ca-certificates.crt")
KEYS = {
    ("ECDSA", 256): "ec-256",
    ("ECDSA", 384): "ec-384",
    ("RSA", 2048): "rsa-2048",
    ("RSA", 3072): "rsa-3072",
    ("RSA", 4096): "rsa-4096",
}
_CERT_BLOCK = rb"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+?-----END CERTIFICATE-----"
_KEY_BLOCK = rb"-----BEGIN (PRIVATE KEY|EC PRIVATE KEY|RSA PRIVATE KEY)-----\s+[A-Za-z0-9+/=\s]+?-----END \1-----"


def utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def checksums(material: dict[str, bytes]) -> dict[str, dict[str, str]]:
    return {name: {"sha256": hashlib.sha256(material[name]).hexdigest()} for name in PEMS}


def read_material(directory: Path) -> dict[str, bytes]:
    return {name: read_regular(directory / name, MAX_FILE) for name in PEMS}


def _certs(data: bytes) -> list[x509.Certificate]:
    if not re.fullmatch(rb"\s*(?:" + _CERT_BLOCK + rb"\s*)+", data):
        raise ValueError("Invalid certificate PEM")
    return x509.load_pem_x509_certificates(data)


def validate(
    identifier: str,
    version: int,
    material: dict[str, bytes],
    scratch: Path,
    trust: Path = TRUST,
) -> dict[str, Any]:
    if set(material) != set(PEMS) or any(len(data) > MAX_FILE for data in material.values()):
        raise ValueError("Invalid certificate files")
    leafs = _certs(material["cert.pem"])
    chain = _certs(material["chain.pem"]) if material["chain.pem"].strip() else []
    if len(leafs) != 1 or _certs(material["fullchain.pem"]) != leafs + chain:
        raise ValueError("Full chain must contain the leaf followed by its intermediates")
    try:
        for child, parent in zip(leafs + chain, chain):
            child.verify_directly_issued_by(parent)
    except (InvalidSignature, UnsupportedAlgorithm, TypeError):
        raise ValueError("Invalid intermediate certificate signature") from None
    leaf = leafs[0]
    try:
        names = list(leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value)
    except x509.ExtensionNotFound:
        raise ValueError("Certificate must contain exactly the requested DNS SAN") from None
    if (
        len(names) != 1
        or not isinstance(names[0], x509.DNSName)
        or normalise_identifier(names[0].value) != identifier
    ):
        raise ValueError("Certificate must contain exactly the requested DNS SAN")
    if not leaf.not_valid_before_utc <= datetime.now(UTC) < leaf.not_valid_after_utc:
        raise ValueError("Certificate is outside its validity period")
    key_data = material["privkey.pem"].strip()
    if not re.fullmatch(_KEY_BLOCK, key_data):
        raise ValueError("Invalid private key PEM")
    try:
        key = serialization.load_pem_private_key(key_data, password=None)
    except (TypeError, UnsupportedAlgorithm):
        raise ValueError("Unsupported private key") from None
    if isinstance(key, ec.EllipticCurvePrivateKey) and isinstance(
        key.curve, (ec.SECP256R1, ec.SECP384R1)
    ):
        algorithm = "ECDSA"
    elif isinstance(key, rsa.RSAPrivateKey):
        algorithm = "RSA"
    else:
        raise ValueError("Unsupported private key algorithm")
    if (algorithm, key.key_size) not in KEYS:
        raise ValueError("Unsupported private key size")
    if key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    ) != leaf.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    ):
        raise ValueError("Private key does not match the certificate")
    with tempfile.TemporaryDirectory(dir=scratch) as temporary:
        directory = Path(temporary)
        leaf_file = directory / "leaf.pem"
        leaf_file.write_bytes(material["cert.pem"])
        args = [
            "/usr/bin/openssl",
            "verify",
            "-purpose",
            "sslserver",
            "-trusted",
            str(trust),
        ]
        if chain:
            chain_file = directory / "chain.pem"
            chain_file.write_bytes(material["chain.pem"])
            args += ["-untrusted", str(chain_file)]
        try:
            result = subprocess.run(
                [*args, str(leaf_file)],
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
                capture_output=True,
                timeout=15,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise ValueError("Certificate trust verification timed out") from None
    if result.returncode:
        raise ValueError("Certificate chain is not trusted for TLS server use")
    return {
        "schema_version": 1,
        "certificate_id": identifier,
        "version": version,
        "serial": f"{leaf.serial_number:X}",
        "issuer": leaf.issuer.rfc4514_string(),
        "not_before": utc(leaf.not_valid_before_utc),
        "not_after": utc(leaf.not_valid_after_utc),
        "key_algorithm": algorithm,
        "key_size": key.key_size,
        "files": checksums(material),
    }


def parse_manifest(data: bytes, identifier: str) -> dict[str, Any]:
    def unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value = dict(items)
        if len(value) != len(items):
            raise ValueError("Duplicate manifest fields")
        return value

    if len(data) > MAX_FILE:
        raise ValueError("Invalid certificate manifest")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique)
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError("Invalid certificate manifest") from None
    fields = {
        "schema_version",
        "certificate_id",
        "version",
        "serial",
        "issuer",
        "not_before",
        "not_after",
        "key_algorithm",
        "key_size",
        "files",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["certificate_id"] != identifier
        or type(value["version"]) is not int
        or not 0 < value["version"] < 2**63
        or not isinstance(value["key_algorithm"], str)
        or type(value["key_size"]) is not int
        or (value["key_algorithm"], value["key_size"]) not in KEYS
        or not isinstance(value["files"], dict)
        or set(value["files"]) != set(PEMS)
        or any(
            not isinstance(entry, dict)
            or set(entry) != {"sha256"}
            or not isinstance(entry["sha256"], str)
            or not re.fullmatch("[a-f0-9]{64}", entry["sha256"])
            for entry in value["files"].values()
        )
        or any(
            not isinstance(value[key], str)
            for key in (
                "serial",
                "issuer",
                "not_before",
                "not_after",
            )
        )
    ):
        raise ValueError("Invalid certificate manifest")
    return value


def unpack_bundle(data: bytes) -> dict[str, bytes]:
    if len(data) > MAX_BUNDLE:
        raise ValueError("Certificate bundle is too large")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) != len(FILES) or {entry.filename for entry in entries} != set(FILES):
                raise ValueError("Unexpected files in certificate bundle")
            if any(
                entry.compress_type != zipfile.ZIP_STORED
                or entry.file_size > MAX_FILE
                or entry.flag_bits & 1
                or (entry.external_attr >> 16) & 0o170000 not in (0, 0o100000)
                for entry in entries
            ):
                raise ValueError("Invalid certificate bundle entry")
            return {entry.filename: archive.read(entry) for entry in entries}
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError):
        raise ValueError("Invalid certificate bundle") from None
