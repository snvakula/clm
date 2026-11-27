import logging
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clm_client_debian.application import Client
from clm_server.certificate import MAX_FILE, parse_manifest
from clm_server.identity import storage_id
from clm_server.locking import publication_lock
from clm_server.api import create_app
from clm_server.route53 import Target
from clm_server.service import Administration

DOMAIN = "git.example.com"
WILDCARD = "*.example.com"


class Engine:
    def __init__(self, root, material):
        self.root = root
        self.material = material
        self.route = self
        self.targets = {}
        self.exports = {}
        self.calls = []
        self.skip = False
        self.fail = False

    def discover(self, identifier):
        return self.targets.get(identifier, Target("first", "acme.example.com", "Z1"))

    def instance(self, account):
        path = self.root / "acme/instances" / account / "config"
        path.mkdir(parents=True, exist_ok=True)
        return path.parent

    def recover_dns(self):
        pass

    def obtain(self, target, identifier, algorithm, *, issue, force):
        self.calls.append((target, identifier, algorithm, issue, force))
        if self.fail:
            raise ValueError("Mock ACME operation failed")
        if self.skip:
            return False
        self.exports[target.account, identifier] = self.material(identifier)
        return True

    def export(self, target, identifier, algorithm):
        return self.exports[target.account, identifier]


@pytest.fixture
def server(tmp_path, pki, monkeypatch):
    trust, material = pki
    root = tmp_path / "server"
    for name in ("data/certificates", "run", "logs"):
        (root / name).mkdir(parents=True)
    lock = root / "run/publication.lock"
    lock.touch(mode=0o600)
    admin = Administration(root)
    admin.repo.trust = trust
    admin.db.initialise()
    engine = Engine(root, material)
    monkeypatch.setattr(admin, "engine", lambda: engine)
    return admin, engine, TestClient(create_app(root))


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def url(identifier, suffix):
    return f"/v1/certificates/{storage_id(identifier)}/{suffix}"


def test_issuance_reuse_rotation_authorisation_and_leases(server):
    admin, engine, http = server
    git, jira, kb = [admin.client_change(name, "add") for name in ("git", "jira", "kb")]
    assert len({git, jira, kb}) == 3
    admin.client_change("git", "grant", DOMAIN)
    admin.client_change("jira", "grant", WILDCARD)
    admin.client_change("kb", "grant", WILDCARD)
    assert len(engine.calls) == 2
    assert all(call[3] for call in engine.calls)
    assert http.get(url(WILDCARD, "versions/1/bundle"), headers=headers(git)).status_code == 404
    assert http.get("/v1/client", headers=headers(git)).json() == {"name": "git"}
    assert http.get("/v1/client?token=" + git).status_code == 401
    assert (
        http.get("/v1/certificates", headers=headers(jira)).json()["certificates"][0][
            "certificate_id"
        ]
        == WILDCARD
    )
    first = http.get(url(WILDCARD, "manifest"), headers=headers(jira))
    assert first.headers["cache-control"] == "no-store"
    assert http.get(url(WILDCARD, "versions/1/bundle"), headers=headers(jira)).content == (
        http.get(url(WILDCARD, "versions/1/bundle"), headers=headers(kb)).content
    )
    old = admin.repo.manifest(WILDCARD, 1)[1]
    engine.skip = True
    assert admin.obtain(WILDCARD, "renew") == 1
    assert engine.calls[-1][3:] == (False, False)
    engine.skip = False
    assert admin.obtain(WILDCARD, "rotate-key") == 2
    assert engine.calls[-1][3:] == (False, True)
    assert (
        old["files"]["privkey.pem"] != admin.repo.manifest(WILDCARD, 2)[1]["files"]["privkey.pem"]
    )
    assert http.get(url(WILDCARD, "versions/1/bundle"), headers=headers(jira)).status_code == 200
    assert http.get(url(WILDCARD, "versions/1/bundle"), headers=headers(kb)).status_code == 404
    admin.client_change("jira", "deny", WILDCARD)
    assert http.get(url(WILDCARD, "versions/1/bundle"), headers=headers(jira)).status_code == 404
    admin.client_change("kb", "disable")
    assert http.get("/v1/client", headers=headers(kb)).status_code == 401
    admin.client_change("kb", "enable")
    new_token = admin.client_change("kb", "rotate-token")
    assert http.get("/v1/client", headers=headers(kb)).status_code == 401
    assert http.get("/v1/client", headers=headers(new_token)).status_code == 200
    assert (
        http.get(
            "/v1/certificates/%2e%2e%2fconfig/manifest", headers=headers(new_token)
        ).status_code
        == 404
    )
    assert http.post("/v1/health").status_code == 405
    assert (admin.repo.files.directory(WILDCARD, 2) / "privkey.pem").stat().st_mode & 0o777 == 0o640
    assert git.encode() not in admin.db.path.read_bytes()


def test_migration_preserves_live_generation_and_recovers_interrupted_commit(server, monkeypatch):
    admin, engine, http = server
    token = admin.client_change("git", "add")
    admin.client_change("git", "grant", DOMAIN)
    engine.targets[DOMAIN] = Target("second", "acme.example.org", "Z2")
    engine.fail = True
    with pytest.raises(ValueError, match="Mock ACME"):
        admin.obtain(DOMAIN, "renew")
    assert admin.repo.certificate(DOMAIN)["account"] == "first"
    engine.fail = False

    original = admin.repo._complete

    def interrupted(entry):
        raise RuntimeError("Interrupted publication")

    monkeypatch.setattr(admin.repo, "_complete", interrupted)
    with pytest.raises(RuntimeError):
        admin.obtain(DOMAIN, "renew")
    assert admin.repo.certificate(DOMAIN)["account"] == "first"
    assert admin.repo.files.current(DOMAIN) == 2
    assert http.get(url(DOMAIN, "versions/1/bundle"), headers=headers(token)).status_code == 200
    monkeypatch.setattr(admin.repo, "_complete", original)
    with publication_lock(admin.repo.lock, write=True):
        admin.repo.reconcile()
    assert admin.repo.certificate(DOMAIN)["account"] == "second"
    assert admin.repo.certificate(DOMAIN)["zone"] == "Z2"
    assert http.get(url(DOMAIN, "versions/2/bundle"), headers=headers(token)).status_code == 200


def test_download_and_global_issuance_locks(server, monkeypatch):
    admin, engine, http = server
    token = admin.client_change("git", "add")
    admin.client_change("git", "grant", DOMAIN)
    started, release = threading.Event(), threading.Event()
    completed = []
    original = engine.obtain

    def delayed(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "obtain", delayed)
    first = threading.Thread(target=lambda: completed.append(admin.obtain(DOMAIN, "renew")))
    first.start()
    assert started.wait(5)
    second = threading.Thread(target=lambda: completed.append(admin.obtain(WILDCARD, "issue")))
    second.start()
    second.join(timeout=0.05)
    assert second.is_alive()
    assert http.get(url(DOMAIN, "versions/1/bundle"), headers=headers(token)).status_code == 200
    release.set()
    first.join(5)
    second.join(5)
    assert sorted(completed) == [1, 2]


class TestRemote:
    __test__ = False

    def __init__(self, http, token):
        self.http = http
        self.headers = headers(token)
        self.downloads = 0
        self.fail = False
        self.modify = lambda payload: payload

    def available(self):
        if self.fail:
            raise ValueError("Server is unavailable")
        response = self.http.get("/v1/certificates", headers=self.headers)
        assert response.status_code == 200
        return sorted(entry["certificate_id"] for entry in response.json()["certificates"])

    def manifest(self, identifier):
        if self.fail:
            raise ValueError("Server is unavailable")
        response = self.http.get(url(identifier, "manifest"), headers=self.headers)
        assert response.status_code == 200
        return parse_manifest(response.content[:MAX_FILE], identifier)

    def bundle(self, identifier, version):
        self.downloads += 1
        response = self.http.get(
            url(identifier, f"versions/{version}/bundle"), headers=self.headers
        )
        assert response.status_code == 200
        return self.modify(response.content)


@pytest.fixture
def installed(tmp_path, pki, server, monkeypatch):
    admin, _, http = server
    token = admin.client_change("git", "add")
    admin.client_change("git", "grant", DOMAIN)
    root = tmp_path / "client"
    for name in ("config/hooks.d", "run", "data", "certificates"):
        (root / name).mkdir(parents=True)
    config = root / "config/client.yaml"
    config.write_text(f"server: https://clm.example.com\ntoken: '{token}'\n")
    config.chmod(0o600)
    client = Client(root, logging.getLogger("test-client"), trust=pki[0])
    remote = TestRemote(http, token)
    from clm_client_debian import application

    def connect(server, supplied, *, timeout):
        assert server == "https://clm.example.com" and supplied == token and timeout == 20
        return remote

    monkeypatch.setattr(application, "Remote", connect)
    state = client.state()
    assert client.update(state) == (1, 1)
    return client, remote, state


def test_install_idempotency_update_and_unavailable_server(server, installed):
    admin, _, _ = server
    client, remote, state = installed
    info = client.inspect(state, DOMAIN)
    assert client.config_file.stat().st_mode & 0o777 == 0o600
    assert Path(info["private_key_path"]).stat().st_mode & 0o777 == 0o600
    client.update(state)
    assert remote.downloads == 1
    admin.obtain(DOMAIN, "renew")
    client.update(state)
    assert remote.downloads == 2
    assert client.inspect(state, DOMAIN)["version"] == 2
    assert client.inspect(state, DOMAIN)["certificate_path"] == info["certificate_path"]
    remote.fail = True
    with pytest.raises(ValueError, match="unavailable"):
        client.update(state)
    saved = client.state()
    assert saved["last_error"]
    assert client.files.current(DOMAIN) == 2
    assert not list((client.root / "run").iterdir())


def test_one_update_fetches_all_grants_and_only_runs_hooks_on_changes(server, installed):
    admin, _, _ = server
    client, remote, state = installed
    admin.client_change("git", "grant", WILDCARD)
    admin.obtain(DOMAIN, "renew")
    for name in ("90-second", "10-first"):
        hook = client.hooks / name
        hook.write_text(
            '#!/bin/sh\nprintf "%s|%s|%s\n" "${0##*/}" "$CLM_HOOK_EVENT" '
            '"$CLM_UPDATED_CERTIFICATES" >>"$CLM_CLIENT_ROOT/events"\n'
        )
        hook.chmod(0o700)
    assert client.update(state) == (2, 2)
    assert client.files.current(DOMAIN) == 2
    assert client.files.current(WILDCARD) == 1
    contents = (client.root / "events").read_text()
    assert contents.splitlines() == [
        "10-first|updated|*.example.com",
        DOMAIN,
        "90-second|updated|*.example.com",
        DOMAIN,
    ]
    assert client.update(state) == (2, 0)
    assert remote.downloads == 3
    assert (client.root / "events").read_text() == contents
    admin.client_change("git", "deny", WILDCARD)
    assert client.update(state) == (1, 0)
    assert client.files.current(WILDCARD) == 1


def test_hook_failure_restores_previous_certificate(server, installed):
    admin, _, _ = server
    client, _, state = installed
    admin.obtain(DOMAIN, "renew")
    hook = client.hooks / "test-hook"
    hook.write_text(
        '#!/bin/sh\nprintf "%s\n" "$CLM_HOOK_EVENT" >>"$CLM_CLIENT_ROOT/events"\n'
        '[ "$CLM_HOOK_EVENT" = rollback ]\n'
    )
    hook.chmod(0o700)
    with pytest.raises(ValueError, match="test-hook failed during updated"):
        client.update(state)
    assert client.files.current(DOMAIN) == 1
    assert client.state()["pending"] is None
    assert not client.files.directory(DOMAIN, 2).exists()
    assert (client.root / "events").read_text().splitlines() == ["updated", "rollback"]


def test_failed_download_prevents_partial_batch_activation(server, installed):
    admin, _, _ = server
    client, remote, state = installed
    admin.client_change("git", "grant", WILDCARD)
    admin.obtain(DOMAIN, "renew")
    count = 0

    def modify(payload):
        nonlocal count
        count += 1
        return payload if count == 1 else b"invalid bundle"

    remote.modify = modify
    with pytest.raises(ValueError, match="Invalid certificate bundle"):
        client.update(state)
    assert count == 2
    assert client.files.current(DOMAIN) == 1
    assert client.files.current(WILDCARD) is None
    assert not client.files.directory(WILDCARD, 1).exists()
    assert client.state()["pending"] is None


def test_client_recovers_killed_update(installed):
    client, _, state = installed
    state["pending"] = {
        "switching": True,
        "changes": {
            DOMAIN: {"old": 1, "new": 2, "services": ["nginx"]},
        },
    }
    from clm_server.certificate import FILES

    version = client.files.directory(DOMAIN, 1)
    client.files.stage(DOMAIN, 2, {name: (version / name).read_bytes() for name in FILES})
    client.save(state)
    client.files.activate(DOMAIN, 2)
    client.recover(client.state())
    assert client.files.current(DOMAIN) == 1
    assert not client.files.directory(DOMAIN, 2).exists()


def test_same_account_change_restores_engine_state(server, monkeypatch):
    admin, engine, _ = server
    admin.obtain(DOMAIN, "issue")
    state = engine.instance("first")
    (state / "certificates").mkdir()
    (state / "work").mkdir()
    account_key = state / "config/mock-account.key"
    account_key.write_text("original account key")
    account_key.chmod(0o600)
    original = engine.obtain

    def damaged(*args, **kwargs):
        account_key.write_text("partially written state")
        raise ValueError("Interrupted engine migration")

    monkeypatch.setattr(engine, "obtain", damaged)
    engine.targets[DOMAIN] = Target("first", "other.example.com", "Z2")
    with pytest.raises(ValueError, match="Interrupted engine migration"):
        admin.obtain(DOMAIN, "renew")
    assert account_key.read_text() == "original account key"
    assert admin.repo.certificate(DOMAIN)["zone"] == "Z1"
    assert admin.repo.files.current(DOMAIN) == 1
    monkeypatch.setattr(engine, "obtain", original)
    assert admin.obtain(DOMAIN, "renew") == 2
    assert admin.repo.certificate(DOMAIN)["zone"] == "Z2"
    assert not list(state.glob("*migration*"))


def test_issuance_repairs_missing_repository_generation(server):
    import shutil

    admin, _, _ = server
    assert admin.obtain(DOMAIN, "issue") == 1
    shutil.rmtree(admin.repo.files.directory(DOMAIN, 1))
    assert admin.obtain(DOMAIN, "issue") == 2
    assert admin.repo.files.current(DOMAIN) == 2


def test_listener_accepts_inherited_unix_socket(server):
    import socket
    import subprocess
    import sys

    admin, _, _ = server
    path = admin.root / "run/test.sock"
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(path))
        listener.listen(5)
        args = [
            sys.executable,
            "-c",
            "from clm_server.api import main; main()",
            "--root",
            str(admin.root),
            "--fd",
            str(listener.fileno()),
        ]
        with subprocess.Popen(
            args, pass_fds=(listener.fileno(),), stderr=subprocess.PIPE
        ) as service:
            try:
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(5)
                    client.connect(str(path))
                    client.sendall(
                        b"GET /v1/health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
                    )
                    with client.makefile("rb") as stream:
                        response = stream.read()
                assert b"HTTP/1.1 200 OK" in response
                assert b"cache-control: no-store" in response.lower()
            finally:
                service.terminate()
                service.wait(timeout=5)


def test_client_rejects_bundle_checksum_mismatch(server, installed):
    from clm_client_debian.certificate import unpack_bundle
    from clm_server.certificate import make_bundle

    admin, _, _ = server
    client, remote, state = installed
    admin.obtain(DOMAIN, "renew")

    def tamper(payload):
        files = unpack_bundle(payload)
        manifest = files.pop("manifest.json")
        files["privkey.pem"] += b"\n"
        return make_bundle(files, manifest)

    remote.modify = tamper
    with pytest.raises(ValueError, match="bundle does not match"):
        client.update(state)
    assert client.files.current(DOMAIN) == 1
    assert not client.files.directory(DOMAIN, 2).exists()


def test_server_cli_status_and_client_listing_keep_json_optional(server, caplog):
    import json
    import subprocess
    import sys

    admin, _, _ = server
    admin.client_change("git", "add")
    with caplog.at_level(logging.INFO, logger="clm"):
        admin.client_change("git", "grant", WILDCARD)
    assert "Looking up the public DNS challenge" in caplog.text
    assert "Saving access for client git" in caplog.text
    admin.repo.ensure(DOMAIN, "ec-256")
    admin.event("issue", identifier=DOMAIN, detail="Missing public CNAME")
    command = [
        sys.executable,
        "-c",
        "from clm_server.cli import main; raise SystemExit(main())",
        "--root",
        str(admin.root),
    ]

    def run(*arguments):
        return subprocess.run([*command, *arguments], check=True, capture_output=True, text=True)

    text = run("status")
    assert not text.stderr
    assert WILDCARD in text.stdout
    assert "Not issued" in text.stdout
    assert "Missing public CNAME" in text.stdout
    assert " UTC" in text.stdout
    assert "certificates=[" not in text.stdout
    structured = run("status", "--json")
    assert not structured.stderr
    report = json.loads(structured.stdout)
    assert report["certificates"][0]["identifier"] == WILDCARD
    assert report["repository_inconsistencies"] == []
    client = run("client", "show", "git")
    assert "- *.example.com" in client.stdout
    assert "['" not in client.stdout
    records = (admin.root / "logs/clm.log").read_text().splitlines()
    assert all(json.loads(line).get("message") for line in records)


def test_client_cli_status_supports_readable_and_json_output(installed, capsys):
    import json

    from clm_client_debian.cli import arguments, dispatch

    client, _, _ = installed
    parser = arguments()
    dispatch(parser.parse_args(["status"]), client)
    stdout, stderr = capsys.readouterr()
    assert DOMAIN in stdout
    assert "Generation" in stdout
    assert " UTC" in stdout
    assert "Certificate path" in stdout
    assert "{" not in stdout
    assert not stderr
    dispatch(parser.parse_args(["status", "--json"]), client)
    stdout, stderr = capsys.readouterr()
    assert json.loads(stdout)["certificates"][0]["identifier"] == DOMAIN
    assert not stderr


def test_client_rejects_a_config_readable_by_other_users(installed):
    client, _, state = installed
    client.config_file.chmod(0o644)
    with pytest.raises(ValueError, match="readable only by their owner"):
        client.update(state)
