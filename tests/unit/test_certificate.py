import io
from contextlib import nullcontext
import zipfile
from pathlib import Path

import pytest
from cryptography import x509

from clm_client_debian.certificate import unpack_bundle
from clm_server.certificate import (
    json_bytes,
    make_bundle,
    parse_manifest,
    validate,
)
from clm_server.filesystem import read_regular


@pytest.mark.parametrize("name", ["example.com", "*.example.com"])
def test_trust_identity_and_archive_round_trip(pki, tmp_path, name):
    trust, material = pki
    pem = material(name)
    manifest = validate(name, 1, pem, tmp_path, trust)
    archive = unpack_bundle(make_bundle(pem, json_bytes(manifest)))
    assert parse_manifest(archive.pop("manifest.json"), name) == manifest
    assert archive == pem


def test_reject_wrong_name_extra_san_expired_wrong_key_and_chain(pki, tmp_path):
    trust, material = pki
    name = "*.example.com"
    valid = material(name)
    wrong_chain = {**valid, "chain.pem": b"", "fullchain.pem": valid["cert.pem"]}
    cases = [
        material("example.com"),
        material(name, extra=[x509.DNSName("example.com")]),
        material(name, expired=True),
        {**valid, "privkey.pem": material(name)["privkey.pem"]},
        {**valid, "fullchain.pem": valid["chain.pem"]},
        wrong_chain,
    ]
    for case in cases:
        with pytest.raises(ValueError):
            validate(name, 1, case, tmp_path, trust)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["trust.pem"]


@pytest.mark.parametrize("path", ["../privkey.pem", "/privkey.pem", "cert.pem", "manifest.json"])
def test_reject_unexpected_bundle_members(pki, path):
    _, material = pki
    output = io.BytesIO(make_bundle(material("example.com"), b"{}"))
    with zipfile.ZipFile(output, "a") as archive:
        with pytest.warns(UserWarning) if path in {"cert.pem", "manifest.json"} else nullcontext():
            archive.writestr(path, b"payload")
    with pytest.raises(ValueError):
        unpack_bundle(output.getvalue())


def test_reject_symlink_and_unsafe_private_file(tmp_path: Path):
    path = tmp_path / "key"
    path.write_text("key")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="owner"):
        read_regular(path, 100, private=True)
    symlink = tmp_path / "link"
    symlink.symlink_to(path)
    with pytest.raises(OSError):
        read_regular(symlink, 100)


def test_manifest_rejects_ambiguous_types(pki, tmp_path):
    trust, material = pki
    name = "example.com"
    manifest = validate(name, 1, material(name), tmp_path, trust)
    for key, value in {"version": True, "files": [], "certificate_id": "*.example.com"}.items():
        with pytest.raises(ValueError):
            parse_manifest(json_bytes({**manifest, key: value}), name)
    with pytest.raises(ValueError):
        parse_manifest(b'{"version":1,"version":2}', name)
