import logging
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clm_client.application import Client
from clm_common.certificate import MAX_FILE, parse_manifest
from clm_common.identity import storage_id
from clm_common.locking import publication_lock
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

    def client(self):
        return self.http.get("/v1/client", headers=self.headers).json()["name"]

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
    for name in ("config/services.d", "run", "data", "certificates"):
        (root / name).mkdir(parents=True)
    (root / "config/services.d/nginx.yaml").write_text(
        "name: nginx\ncheck: [/bin/true]\nreload: [/bin/true]\nverify: [/bin/true]\n"
    )
    client = Client(root, logging.getLogger("test-client"), trust=pki[0])
    remote = TestRemote(http, token)
    monkeypatch.setattr(client, "_remote", lambda server, token: remote)
    assert client.initialise("https://clm.example.com", token) == "git"
    state = client.state()
    client.apply(state, [DOMAIN], service_names=["nginx"])
    return client, remote, state


def test_install_idempotency_update_and_unavailable_server(server, installed):
    admin, _, _ = server
    client, remote, state = installed
    info = client.inspect(state, DOMAIN)
    assert client.credentials.stat().st_mode & 0o777 == 0o600
    assert Path(info["private_key_path"]).stat().st_mode & 0o777 == 0o600
    client.apply(state, [DOMAIN])
    assert remote.downloads == 1
    admin.obtain(DOMAIN, "renew")
    client.apply(state, [DOMAIN])
    assert remote.downloads == 2
    assert client.inspect(state, DOMAIN)["version"] == 2
    assert client.inspect(state, DOMAIN)["certificate_path"] == info["certificate_path"]
    remote.fail = True
    with pytest.raises(ValueError, match="unavailable"):
        client.apply(state, [DOMAIN])
    saved = client.state()
    assert saved["last_error"]
    assert client.files.current(DOMAIN) == 2
    assert not list((client.root / "run").iterdir())


@pytest.mark.parametrize("operation", ["check", "reload", "verify"])
def test_activation_failure_recovers_previous_generation(server, installed, monkeypatch, operation):
    admin, _, _ = server
    client, _, state = installed
    admin.obtain(DOMAIN, "renew")
    calls = []

    def commands(profile, action):
        calls.append((profile["name"], action))
        if action == operation and client.files.current(DOMAIN) == 2:
            raise ValueError("Mock activation failed")

    monkeypatch.setattr(client.profiles, "execute", commands)
    with pytest.raises(ValueError, match="Mock activation"):
        client.apply(state, [DOMAIN])
    assert client.files.current(DOMAIN) == 1
    assert client.state()["pending"] is None
    assert not client.files.directory(DOMAIN, 2).exists()
    assert calls[-3:] == [("nginx", "check"), ("nginx", "reload"), ("nginx", "verify")]


def test_client_recovers_killed_update_and_removal(installed):
    client, _, state = installed
    state["pending"] = {
        "switching": True,
        "changes": {
            DOMAIN: {"old": 1, "new": 2, "services": ["nginx"]},
        },
    }
    from clm_common.certificate import FILES

    version = client.files.directory(DOMAIN, 1)
    client.files.stage(DOMAIN, 2, {name: (version / name).read_bytes() for name in FILES})
    client.save(state)
    client.files.activate(DOMAIN, 2)
    client.recover(client.state())
    assert client.files.current(DOMAIN) == 1
    assert not client.files.directory(DOMAIN, 2).exists()
    with pytest.raises(ValueError, match="associated"):
        client.remove(client.state(), DOMAIN, False)
    client.remove(client.state(), DOMAIN, True)
    assert client.files.current(DOMAIN) is None


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
    from clm_common.certificate import make_bundle, unpack_bundle

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
        client.apply(state, [DOMAIN])
    assert client.files.current(DOMAIN) == 1
    assert not client.files.directory(DOMAIN, 2).exists()
