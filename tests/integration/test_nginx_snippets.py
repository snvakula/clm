import hashlib
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from client_linux import filesystem, identity

HOOK = Path(__file__).resolve().parents[2] / "examples/client/hooks.d/nginx-snippets"


@pytest.fixture
def hook(monkeypatch):
    monkeypatch.setitem(sys.modules, "app.filesystem", filesystem)
    monkeypatch.setitem(sys.modules, "app.identity", identity)
    return runpy.run_path(str(HOOK))["sync_snippets"]


def activate(root, identifier, number):
    base = root.joinpath("certificates", *identity.storage_path(identifier))
    generation = base / "versions" / f"{number:06d}"
    generation.mkdir(parents=True)
    for name in ("fullchain.pem", "privkey.pem"):
        (generation / name).write_text("test certificate material")
    active = base / "current"
    active.unlink(missing_ok=True)
    active.symlink_to(generation.relative_to(base))
    return active


def test_snippets_have_stable_paths_for_exact_wildcard_and_long_names(hook, tmp_path):
    root = tmp_path / "client"
    snippets = tmp_path / "nginx/snippets/clm"
    long_identifier = ".".join(["a" * 63] * 3 + ["b" * 61])
    long_snippet = f"_sha256-{hashlib.sha256(long_identifier.encode()).hexdigest()}.conf"
    expected = {
        "example.com": "example.com.conf",
        "wildcard.example.com": "wildcard.example.com.conf",
        "*.example.com": "_wildcard.example.com.conf",
        long_identifier: long_snippet,
    }
    paths = {identifier: activate(root, identifier, 1) for identifier in expected}
    hook(root, snippets, list(expected), rollback=False)
    assert {entry.name for entry in snippets.iterdir()} == set(expected.values())
    for identifier, filename in expected.items():
        path = snippets / filename
        active = paths[identifier]
        content = path.read_text()
        assert content.splitlines()[1:] == [
            f"ssl_certificate {active}/fullchain.pem;",
            f"ssl_certificate_key {active}/privkey.pem;",
        ]
        assert path.stat().st_mode & 0o777 == 0o644
        original_inode = path.stat().st_ino
        activate(root, identifier, 2)
        hook(root, snippets, [identifier], rollback=False)
        assert path.stat().st_ino == original_inode
        assert path.read_text() == content


def test_rollback_removes_only_generated_snippets_without_an_active_certificate(hook, tmp_path):
    root = tmp_path / "client"
    snippets = tmp_path / "nginx/snippets/clm"
    first = activate(root, "first.example.com", 1)
    kept = activate(root, "kept.example.com", 1)
    operator = activate(root, "operator.example.com", 1)
    hook(root, snippets, ["first.example.com", "kept.example.com"], rollback=False)
    foreign = snippets / "operator.example.com.conf"
    foreign.write_text("# Operator-managed file\n")
    with pytest.raises(ValueError, match="Refusing to overwrite"):
        hook(root, snippets, ["operator.example.com"], rollback=False)

    first.unlink()
    operator.unlink()
    hook(
        root,
        snippets,
        ["first.example.com", "kept.example.com", "operator.example.com"],
        rollback=True,
    )
    assert not (snippets / "first.example.com.conf").exists()
    assert str(kept) in (snippets / "kept.example.com.conf").read_text()
    assert foreign.read_text() == "# Operator-managed file\n"


def test_nginx_loads_the_generated_snippet(hook, tmp_path, pki):
    nginx = os.environ.get("CLM_TEST_NGINX") or shutil.which("nginx")
    if nginx is None:
        pytest.skip("Nginx is required to validate a generated snippet")
    root = tmp_path / "client"
    snippets = tmp_path / "snippets"
    identifier = "example.com"
    active = activate(root, identifier, 1)
    _, material = pki
    for name, content in material(identifier).items():
        (active / name).write_bytes(content)
    hook(root, snippets, [identifier], rollback=False)

    config = tmp_path / "nginx.conf"
    temp_paths = "".join(
        f"{name}_temp_path {tmp_path / name};"
        for name in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi")
    )
    config.write_text(
        f"pid {tmp_path / 'nginx.pid'}; error_log stderr; events {{}} "
        f"http {{ {temp_paths} access_log off; "
        f"server {{ listen 18443 ssl; include {snippets / 'example.com.conf'}; }} }}"
    )
    subprocess.run([nginx, "-t", "-p", str(tmp_path), "-c", str(config)], check=True)
