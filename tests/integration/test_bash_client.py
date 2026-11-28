from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from clm_server.certificate import json_bytes, make_bundle, validate
from clm_server.identity import storage_id

REPO = Path(__file__).resolve().parents[2]
CLIENT = REPO / "src/clm_client_bash/clm-client-bash"
DOMAIN = "*.example.com"
TOKEN = "A" * 43


@pytest.fixture
def linux_client(tmp_path, pki):
    for tool in ("bash", "curl", "jq", "openssl", "unzip", "flock"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} is required for Bash client tests")
    trust, material = pki
    root, fixtures, tools = (tmp_path / name for name in ("client", "responses", "tools"))
    for directory in (
        fixtures,
        tools,
        *(root / sub for sub in ("config/hooks.d", "run", "data", "certificates")),
    ):
        directory.mkdir(parents=True)
    config = root / "config/client.conf"
    config.write_text(f"server=https://clm.example.com\ntoken={TOKEN}\n")
    config.chmod(0o600)
    curl = tools / "curl"
    curl.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "assert '-L' not in args and '--location' not in args\n"
        "assert args[args.index('--header') + 1] == '@-'\n"
        f"assert sys.stdin.read() == 'Authorization: Bearer {TOKEN}\\n'\n"
        f"assert not any('{TOKEN}' in arg for arg in args)\n"
        "assert args[-1].startswith('https://clm.example.com/v1/')\n"
        "fixtures = pathlib.Path(os.environ['FIXTURES'])\n"
        "with (fixtures / 'requests').open('a') as requests:\n"
        "    requests.write(args[-1] + '\\n')\n"
        "resource = args[-1].rsplit('/', 1)[1]\n"
        "payload = fixtures / resource\n"
        "destination = pathlib.Path(args[args.index('--output') + 1])\n"
        "destination.write_bytes(payload.read_bytes() if payload.is_file() else b'')\n"
        "status = fixtures / 'status'\n"
        "print(status.read_text() if status.exists() else '200', end='')\n"
    )
    curl.chmod(0o700)
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "CLM_BASH_ROOT": str(root),
        "SSL_CERT_FILE": str(trust),
        "FIXTURES": str(fixtures),
    }

    def run(*args, check=True, overrides=None):
        return subprocess.run(
            [str(CLIENT), *args],
            env={**env, **(overrides or {})},
            check=check,
            capture_output=True,
            text=True,
        )

    def publish(number, files=None):
        material_files = files or material(DOMAIN)
        payload = validate(DOMAIN, number, material_files, tmp_path, trust)
        (fixtures / "manifest").write_bytes(json_bytes(payload))
        (fixtures / "bundle").write_bytes(make_bundle(material_files, json_bytes(payload)))
        (fixtures / "certificates").write_text(
            json.dumps({"certificates": [{"certificate_id": DOMAIN}]})
        )
        return payload

    return root, fixtures, run, publish


def test_bash_client_installs_validated_generation_and_skips_unchanged(linux_client):
    root, fixtures, run, publish = linux_client
    assert (
        run("version").stdout.strip()
        == tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    )
    assert json.loads(run("status", "--json").stdout)["certificates"] == []
    published = publish(1)
    assert "Updated 1" in run("update").stdout
    current = root / "certificates" / storage_id(DOMAIN) / "current"
    assert str(current.readlink()) == "versions/000001"
    assert (current / "privkey.pem").stat().st_mode & 0o777 == 0o640
    assert current.stat().st_mode & 0o777 == 0o750
    status = json.loads(run("status", "--json").stdout)
    assert status["certificates"][0]["expires"] == published["not_after"]
    assert status["certificates"][0]["identifier"] == DOMAIN
    assert "Updated 0" in run("update").stdout
    assert (fixtures / "requests").read_text().count("/bundle\n") == 1
    (fixtures / "certificates").write_text('{"certificates":[]}')
    run("update")
    assert current.is_symlink()


def test_bash_client_rolls_back_hook_and_rejects_redirect(linux_client):
    root, fixtures, run, publish = linux_client
    publish(1)
    run("update")
    publish(2)
    hook = root / "config/hooks.d/test-hook"
    hook.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$CLM_HOOK_EVENT" >>"$CLM_CLIENT_ROOT/hooks.log"\n'
        '[ "$CLM_HOOK_EVENT" = rollback ]\n'
    )
    hook.chmod(0o700)
    assert run("update", check=False).returncode != 0
    assert (root / "hooks.log").read_text().splitlines() == ["updated", "rollback"]
    status = json.loads(run("status", "--json").stdout)
    assert status["certificates"][0]["generation"] == 1
    assert not status["interrupted_update"]
    (fixtures / "status").write_text("302")
    result = run("update", check=False)
    assert "HTTP 302" in result.stderr
    assert TOKEN not in result.stderr + result.stdout


def test_bash_client_rejects_unexpected_bundle_members(linux_client):
    import io
    import zipfile

    _, fixtures, run, publish = linux_client
    publish(1)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../privkey.pem", "test key")
    (fixtures / "bundle").write_bytes(archive.getvalue())
    result = run("update", check=False)
    assert result.returncode != 0
    assert "Unexpected files" in result.stderr
    assert json.loads(run("status", "--json").stdout)["certificates"] == []


def test_bash_client_transfers_a_bundle_over_local_tls(linux_client, pki):
    root, fixtures, run, publish = linux_client
    trust, material = pki
    publish(1)
    tls_files = material("localhost")
    chain = fixtures / "tls-chain.pem"
    chain.write_bytes(tls_files["fullchain.pem"])
    key = fixtures / "tls-key.pem"
    key.touch(mode=0o600)
    key.write_bytes(tls_files["privkey.pem"])
    responses = {
        "/v1/certificates": "certificates",
        f"/v1/certificates/{storage_id(DOMAIN)}/manifest": "manifest",
        f"/v1/certificates/{storage_id(DOMAIN)}/versions/1/bundle": "bundle",
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self.send_error(401)
                return
            if self.path not in responses:
                self.send_error(404)
                return
            payload = (fixtures / responses[self.path]).read_bytes()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as http:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(chain, key)
        http.socket = context.wrap_socket(http.socket, server_side=True)
        worker = threading.Thread(target=http.serve_forever)
        worker.start()
        try:
            port = http.server_address[1]
            (root / "config/client.conf").write_text(
                f"server=https://localhost:{port}\ntoken={TOKEN}\n"
            )
            assert (
                "Updated 1"
                in run(
                    "update", overrides={"PATH": os.environ["PATH"], "CURL_CA_BUNDLE": str(trust)}
                ).stdout
            )
        finally:
            http.shutdown()
            worker.join()


def test_bash_client_recovers_interrupted_activation(linux_client):
    root, _, run, publish = linux_client
    publish(1)
    run("update")
    store = root / "certificates"
    key = storage_id(DOMAIN)
    base = store / key
    shutil.copytree(base / "versions/000001", base / "versions/000002")
    current = base / "current"
    current.unlink()
    current.symlink_to("versions/000002")
    transaction = store / ".transaction"
    transaction.mkdir(mode=0o700)
    (transaction / "plan").write_text(f"{DOMAIN}|{key}|000001|000002\n")
    assert json.loads(run("status", "--json").stdout)["interrupted_update"]
    assert "Updated 0" in run("update").stdout
    assert current.readlink() == Path("versions/000001")
    assert not (base / "versions/000002").exists()
    assert not transaction.exists()


def test_bash_installer_preserves_configuration_and_hooks(tmp_path):
    root = tmp_path / "opt/clm-client-bash"
    local_bin = tmp_path / "usr/local/bin"
    command = [
        "bash",
        "-euc",
        'source "$1"; install_files "$2" "$3" "$4"',
        "test",
        str(REPO / "scripts/install-client-bash.sh"),
        str(REPO),
        str(root),
        str(local_bin),
    ]
    subprocess.run(command, check=True)
    config = root / "config/client.conf"
    hook = root / "config/hooks.d/restart-web-servers"
    certificate_path = root / "certificates"
    assert config.stat().st_mode & 0o777 == 0o600
    assert hook.stat().st_mode & 0o777 == 0o700
    assert certificate_path.stat().st_mode & 0o777 == 0o750
    config.write_text("operator configuration")
    hook.chmod(0o600)
    installed = certificate_path / "pem"
    installed.write_text("existing test certificate")
    subprocess.run(command, check=True)
    assert config.read_text() == "operator configuration"
    assert hook.stat().st_mode & 0o777 == 0o600
    assert installed.read_text() == "existing test certificate"
    assert (local_bin / "clm-client-bash").resolve() == root / "bin/clm-client-bash"
