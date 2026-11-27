import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALLERS = {"server": "install-server.sh", "client": "install-client-debian.sh"}
REVISION = "5567d7fe7298fd37aca53194885ba444c6678669"
TARBALL_SHA256 = "22ca219a96d643910f79d2888d735a444672e5bc0179d3eb6142f0642a9682dc"


def test_scripts_and_timer_parameters():
    scripts = ROOT / "scripts"
    assert {path.name for path in scripts.iterdir()} == set(INSTALLERS.values())
    for path in scripts.glob("*.sh"):
        subprocess.run(["bash", "-n", str(path)], check=True)
    units = ROOT / "packaging/systemd"
    schedules = {
        "clm-renew": ("02:00:00", "30min", "clm cert renew --all"),
        "clm-client-update": ("04:00:00", "3h", "clm-client update"),
    }
    for name, (hour, delay, entrypoint) in schedules.items():
        timer = (units / f"{name}.timer").read_text()
        assert f"OnCalendar=*-*-* {hour} UTC" in timer
        assert f"RandomizedDelaySec={delay}" in timer and "Persistent=true" in timer
        assert entrypoint in (units / f"{name}.service").read_text()
        if shutil.which("systemd-analyze"):
            subprocess.run(
                ["systemd-analyze", "calendar", f"*-*-* {hour} UTC"],
                check=True,
                stdout=subprocess.DEVNULL,
            )
    api = (units / "clmd.service").read_text()
    assert {"User=clm", "Group=clm"} <= set(api.splitlines())
    assert "InaccessiblePaths=/opt/clm/acme /opt/clm/config" in api
    assert "ReadOnlyPaths=/opt/clm/data/certificates" in api
    assert "RestrictAddressFamilies=AF_UNIX" in api


def stage_files(directory, role):
    subprocess.run(
        [
            "bash",
            "-euc",
            f'source "$1"; stage_{role}_files "$2" "$3" "$4"',
            "test",
            str(ROOT / f"scripts/{INSTALLERS[role]}"),
            str(ROOT),
            str(directory),
            sys.executable,
        ],
        check=True,
    )


def test_pinned_acme_patch_and_offline_recovery(tmp_path):
    fixture = os.environ.get("CLM_TEST_ACME_ARCHIVE")
    if fixture is None:
        pytest.skip("Set CLM_TEST_ACME_ARCHIVE to the pinned upstream source archive")
    archive = Path(fixture)
    stage_files(tmp_path, "server")
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == TARBALL_SHA256
    subprocess.run(
        [
            "tar",
            "-xf",
            str(archive),
            "-C",
            str(tmp_path),
            "--strip-components=1",
            f"acme.sh-{REVISION}/acme.sh",
            f"acme.sh-{REVISION}/dnsapi/dns_aws.sh",
        ],
        check=True,
    )
    with (ROOT / "packaging/acme/dns-aws.patch").open("rb") as change:
        subprocess.run(
            ["patch", "--batch", "--fuzz=0", "-p1"], cwd=tmp_path, stdin=change, check=True
        )
    result = subprocess.run(
        [str(tmp_path / "acme.sh"), "--version"],
        check=True,
        capture_output=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert b"3.1.7" in result.stdout
    subprocess.run(
        [
            "/bin/sh",
            str(ROOT / "packaging/acme/recover-dns.sh"),
            str(tmp_path),
        ],
        check=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "CLM_PYTHON": sys.executable,
            "CLM_DNS_JOURNAL": str(tmp_path / "dns-operation.json"),
            "PYTHONPATH": str(tmp_path),
            "CLM_DNS_MODULE": "app.dns_records",
        },
    )

    state = tmp_path / "dns-operation.json"
    zone = "_acme-challenge.acme.example.com"
    challenge = "X" * 43
    common = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "CLM_PYTHON": sys.executable,
        "CLM_DNS_JOURNAL": str(state),
        "CLM_HOSTED_ZONE": "Z1",
        "CLM_CHALLENGE_RECORD": zone,
        "PYTHONPATH": str(tmp_path),
        "CLM_DNS_MODULE": "app.dns_records",
        "AWS_ACCESS_KEY_ID": "MOCK",
        "AWS_SECRET_ACCESS_KEY": "MOCK",
    }
    shim = tmp_path / "mock-python"
    shim.write_text(
        f"#!{sys.executable}\n"
        + """import json
import os
import sys
from pathlib import Path
from xml.etree import ElementTree as ET
from app import dns_records as dns

assert sys.argv[1:3] == ["-m", "app.dns_records"]
del sys.argv[1:3]
mode = sys.argv[1]
record = os.environ["CLM_CHALLENGE_RECORD"]
database = Path(os.environ["HOME"]) / "mock-route53.json"
if mode != "request":
    dns.public_nameservers = lambda name: ["ns.example.net"] if name == "example.com" else []
    sys.exit(dns.main())
method, path, query = sys.argv[2:]
assert path.startswith("2013-04-01/hostedzone/Z1")
if path.endswith("Z1"):
    print(f'<GetHostedZoneResponse xmlns="{dns.NS}"><HostedZone>'
          '<Id>/hostedzone/Z1</Id><Name>example.com.</Name>'
          '<Config><PrivateZone>false</PrivateZone></Config></HostedZone>'
          '<DelegationSet><NameServers><NameServer>ns.example.net</NameServer></NameServers>'
          '</DelegationSet></GetHostedZoneResponse>')
elif method == "GET":
    values = json.loads(database.read_text()) if database.exists() else ['"existing"']
    payload = ''.join(f'<ResourceRecord><Value>{text}</Value></ResourceRecord>' for text in values)
    print(f'<ListResourceRecordSetsResponse xmlns="{dns.NS}"><ResourceRecordSets>'
          f'<ResourceRecordSet><Name>{record}.</Name><Type>TXT</Type><TTL>900</TTL>'
          f'<ResourceRecords>{payload}</ResourceRecords></ResourceRecordSet>'
          '</ResourceRecordSets></ListResourceRecordSetsResponse>')
else:
    xml = ET.fromstring(sys.stdin.buffer.read())
    changes = list(xml.iter(dns.tag("Change")))
    assert [item.findtext(dns.tag("Action")) for item in changes] == ["DELETE", "CREATE"]
    assert all(item.findtext(f".//{dns.tag('TTL')}") == "900" for item in changes)
    values = [item.text for item in changes[1].iter(dns.tag("Value"))]
    database.write_text(json.dumps(values))
    if os.environ.get("MOCK_LOST_RESPONSE"):
        sys.exit(1)
    print("<ChangeResourceRecordSetsResponse/>")
"""
    )
    shim.chmod(0o700)
    common["CLM_PYTHON"] = str(shim)
    result = subprocess.run(
        [
            "/bin/sh",
            "-c",
            '. "$1"; dns_aws_add "$CLM_CHALLENGE_RECORD" "$2"',
            "test",
            str(tmp_path / "dnsapi/dns_aws.sh"),
            challenge,
        ],
        env={**common, "MOCK_LOST_RESPONSE": "1"},
        check=False,
    )
    assert result.returncode == 1
    assert state.exists()
    mock_rrset = tmp_path / "mock-route53.json"
    assert '"existing"' in mock_rrset.read_text().replace("\\", "")
    subprocess.run(
        ["/bin/sh", str(ROOT / "packaging/acme/recover-dns.sh"), str(tmp_path)],
        env=common,
        check=True,
    )
    assert not state.exists()
    import json

    assert json.loads(mock_rrset.read_text()) == ['"existing"']


def test_nginx_configuration_with_debian_binary(tmp_path, pki):
    nginx = os.environ.get("CLM_TEST_NGINX") or shutil.which("nginx")
    if nginx is None:
        pytest.skip("Nginx is required to validate the bundled proxy configuration")
    _, material = pki
    for name, content in material("clm.example.com").items():
        (tmp_path / name).write_bytes(content)
    proxy = (ROOT / "packaging/nginx/clm.conf").read_text()
    proxy = proxy.replace("/path/to/clm-https-fullchain.pem", str(tmp_path / "fullchain.pem"))
    proxy = proxy.replace("/path/to/clm-https-key.pem", str(tmp_path / "privkey.pem"))
    proxy = proxy.replace("443 ssl", "18443 ssl")
    config = tmp_path / "nginx.conf"
    config.write_text(
        f"pid {tmp_path / 'nginx.pid'}; error_log stderr; events {{}} http {{ {proxy} }}"
    )
    for name in ("client_body_temp", "proxy_temp", "fastcgi_temp", "uwsgi_temp", "scgi_temp"):
        # Debian's binary is compiled with system paths, which require root.
        block = f"{name}_path {tmp_path / name};"
        text = config.read_text().replace("http {", "http { " + block, 1)
        config.write_text(text)
    subprocess.run([nginx, "-t", "-p", str(tmp_path), "-c", str(config)], check=True)


@pytest.mark.parametrize("role", ["server", "client"])
def test_installer_writes_active_configuration_and_preserves_edits(tmp_path, role):
    config = tmp_path / "config"
    config.mkdir()
    site = tmp_path / "sites-available/clm"
    site.parent.mkdir()
    command = [
        "bash",
        "-eu",
        "-c",
        'source "$1"; "install_${2}_configuration" "$3" "$4" "$5"',
        "test",
        str(ROOT / f"scripts/{INSTALLERS[role]}"),
        role,
        str(ROOT),
        str(config),
        str(site),
    ]
    subprocess.run(command, check=True)
    if role == "server":
        expected = {
            config / "clm.yaml": 0o600,
            config / "route53.yaml": 0o600,
            config / "aws/credentials": 0o600,
            site: 0o644,
        }
        from clm_server.config import load_config

        assert load_config(config / "route53.yaml") == {"accounts": {}, "challenge_aliases": {}}
        assert "#     credentials_profile: secondary" in (config / "route53.yaml").read_text()
        assert not (config / "aws/config").exists()
        assert "aws_secret_access_key =" in (config / "aws/credentials").read_text()
    else:
        expected = {
            config / "client.yaml": 0o600,
            config / "hooks.d/restart-web-servers": 0o700,
        }
        assert not (config / "services.d").exists()
        assert not (config / "credentials").exists()

    assert not list(tmp_path.rglob("*.example"))
    for path, mode in expected.items():
        assert path.is_file()
        assert path.stat().st_mode & 0o777 == mode
        path.write_text("operator configuration\n")
    subprocess.run(command, check=True)
    for path in expected:
        assert path.read_text() == "operator configuration\n"


def test_installer_refreshes_untouched_legacy_aws_configuration(tmp_path):
    config = tmp_path / "config"
    (config / "aws").mkdir(parents=True)
    (config / "aws/config").touch()
    (config / "route53.yaml").write_text("accounts: {}\nchallenge_aliases: {}\n")
    command = [
        "bash",
        "-euc",
        'source "$1"; install_server_configuration "$2" "$3" "$4"',
        "test",
        str(ROOT / "scripts/install-server.sh"),
        str(ROOT),
        str(config),
        str(tmp_path / "nginx-site"),
    ]
    subprocess.run(command, check=True)
    assert (config / "route53.yaml").read_bytes() == (ROOT / "packaging/route53.yaml").read_bytes()
    assert not (config / "aws/config").exists()
    (config / "aws/config").write_text("[profile local]\n")
    subprocess.run(command, check=True)
    assert (config / "aws/config").read_text() == "[profile local]\n"


def test_flat_layout_migration_keeps_account_and_certificate_data(tmp_path):
    root = tmp_path / "clm"
    workspace = root / "run/install"
    old_release = "a" * 64
    old_app = root / "app/releases" / old_release
    old_app.mkdir(parents=True)
    (root / "app/current").symlink_to(old_app)
    (root / "app/previous").symlink_to(old_app)
    old_venv = root / "venv" / old_release
    old_venv.mkdir(parents=True)
    (old_venv / "pyvenv.cfg").touch()
    (root / "venv/pyvenv.cfg").write_text("standard virtual environment")
    (workspace / "app").mkdir(parents=True)
    (workspace / "app/cli.py").write_text("# Standalone application")
    engine = root / "acme/engines" / f"{'b' * 40}-{old_release}"
    engine.mkdir(parents=True)
    (engine / "acme.sh").touch()
    (root / "acme/engine").symlink_to(engine)
    (root / "acme/acme.sh").write_text("flat executable")
    protected = {
        "data/certificates/primary.pem": "existing certificate",
        "config/aws/credentials": "existing credentials",
        "acme/instances/primary/config/account.key": "existing account key",
    }
    for name, contents in protected.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    command = [
        "bash",
        "-euc",
        'source "$1"; publish_application "$2/app" "$3"; remove_legacy_layout "$2"',
        "test",
        str(ROOT / "scripts/install-server.sh"),
        str(root),
        str(workspace),
    ]
    subprocess.run(command, check=True)
    assert sorted(path.name for path in (root / "app").iterdir()) == ["cli.py"]
    assert sorted(path.name for path in (root / "venv").iterdir()) == ["pyvenv.cfg"]
    assert sorted(path.name for path in (root / "acme").iterdir()) == ["acme.sh", "instances"]
    for name, contents in protected.items():
        assert (root / name).read_text() == contents


@pytest.mark.parametrize(
    ("role", "commands"),
    [("server", {"clm", "clmd"}), ("client", {"clm-client"})],
)
def test_stage_installs_only_the_selected_package_and_commands(tmp_path, role, commands):
    stage_files(tmp_path, role)
    app = tmp_path / "app"
    source = ROOT / "src" / ("clm_server" if role == "server" else "clm_client_debian")
    assert {entry.name for entry in app.iterdir()} == {file.name for file in source.glob("*.py")}
    assert {entry.name for entry in (tmp_path / "bin").iterdir()} == commands
    for name in commands:
        args = ["--help"] if name == "clmd" else ["version"]
        result = subprocess.run(
            [sys.executable, str(tmp_path / "bin" / name), *args],
            env={**os.environ, "PYTHONPATH": str(tmp_path)},
            check=True,
            capture_output=True,
            text=True,
            cwd=tmp_path,
        )
        assert result.stdout
        if name != "clmd":
            import tomllib

            metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
            assert result.stdout.strip() == metadata["project"]["version"]


def test_client_certificate_directory_permissions_on_install_and_upgrade(tmp_path):
    command = [
        "bash",
        "-euc",
        'source "$1"; umask 077; install_client_storage "$2"',
        "test",
        str(ROOT / "scripts/install-client-debian.sh"),
        str(tmp_path),
    ]
    certificates = tmp_path / "certificates"
    subprocess.run(command, check=True)
    assert certificates.stat().st_mode & 0o777 == 0o750

    certificates.chmod(0o700)
    generation = certificates / "generation"
    generation.mkdir(mode=0o700)
    key = generation / "privkey.pem"
    key.write_text("test key")
    key.chmod(0o600)

    for _ in range(2):
        subprocess.run(command, check=True)
        assert certificates.stat().st_mode & 0o777 == 0o750
        assert (tmp_path / "data").stat().st_mode & 0o777 == 0o700
        assert (tmp_path / "logs").stat().st_mode & 0o777 == 0o700
        assert generation.stat().st_mode & 0o777 == 0o700
        assert key.stat().st_mode & 0o777 == 0o600
        assert key.read_text() == "test key"


def test_client_upgrade_replaces_nested_package_and_keeps_registration(tmp_path):
    root = tmp_path / "clm-client"
    workspace = tmp_path / "stage"
    workspace.mkdir()
    (root / "app/clm_client").mkdir(parents=True)
    (root / "config").mkdir()
    credentials = root / "config/credentials"
    credentials.write_text("existing client registration")
    (root / "data").mkdir()
    state = root / "data/state.json"
    state.write_text("existing client certificate state")

    stage_files(workspace, "client")
    subprocess.run(
        [
            "bash",
            "-euc",
            'source "$1"; publish_application "$2/app" "$3"; remove_legacy_layout "$2"',
            "test",
            str(ROOT / "scripts/install-client-debian.sh"),
            str(root),
            str(workspace),
        ],
        check=True,
    )
    assert (root / "app/cli.py").is_file()
    assert not (root / "app/clm_client").exists()
    assert credentials.read_text() == "existing client registration"
    assert state.read_text() == "existing client certificate state"


def test_restart_web_servers_checks_activity_and_restarts_only_running_units(tmp_path):
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(
        '#!/bin/sh\nprintf "%s\n" "$*" >> "$SYSTEMCTL_LOG"\n'
        'case "$*" in\n'
        '  "is-active --quiet apache2.service") exit 0 ;;\n'
        '  "is-active --quiet nginx.service") exit 3 ;;\n'
        '  "show --property=SubState --value apache2.service") echo running ;;\n'
        '  "try-restart apache2.service") exit 0 ;;\n'
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    systemctl.chmod(0o700)
    log = tmp_path / "systemctl.log"
    subprocess.run(
        ["/bin/sh", str(ROOT / "examples/client/hooks.d/restart-web-servers")],
        env={"PATH": str(tmp_path), "SYSTEMCTL_LOG": str(log)},
        check=True,
    )
    assert log.read_text().splitlines() == [
        "is-active --quiet apache2.service",
        "show --property=SubState --value apache2.service",
        "try-restart apache2.service",
        "is-active --quiet nginx.service",
    ]


@pytest.mark.parametrize(
    ("role", "target"),
    [
        ("server", "/opt/clm/bin/clm"),
        ("server", "/opt/clm/bin/clmd"),
        ("client", "/opt/clm-client/bin/clm-client"),
    ],
)
def test_installer_creates_cli_links_and_preserves_conflicts(tmp_path, role, target):
    link = tmp_path / target.rsplit("/", 1)[1]
    command = [
        "bash",
        "-euc",
        'source "$1"; install_cli_link "$2" "$3"',
        "test",
        str(ROOT / "scripts" / INSTALLERS[role]),
        str(link),
        target,
    ]
    subprocess.run(command, check=True)
    subprocess.run(command, check=True)
    assert str(link.readlink()) == target

    link.unlink()
    link.symlink_to("/usr/bin/operator-command")
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert str(link.readlink()) == "/usr/bin/operator-command"

    link.unlink()
    link.write_text("operator command\n")
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert link.read_text() == "operator command\n"

    link.unlink()
    link.mkdir()
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert not list(link.iterdir())


def test_server_identity_migrates_to_clm_without_recreating_accounts(tmp_path):
    subprocess.run(
        [
            "bash",
            "-euc",
            """
source "$1"
user=clm-api
group=clm-api
id() {
    case "$*" in
        "$user") return 0 ;;
        "-u $user") echo 987 ;;
        "-gn $user") echo "$group" ;;
        *) return 1 ;;
    esac
}
getent() { [[ $1 = group && $2 = "$group" ]]; }
useradd() { return 1; }
groupadd() { return 1; }
usermod() { [[ "$*" = "--login clm clm-api" ]] && user=clm; }
groupmod() { [[ "$*" = "--new-name clm clm-api" ]] && group=clm; }
configure_server_identity "$2"
configure_server_identity "$2"
[[ $user = clm && $group = clm ]]
""",
            "test",
            str(ROOT / "scripts/install-server.sh"),
            str(tmp_path),
        ],
        check=True,
    )


@pytest.mark.parametrize(
    ("role", "timer", "units"),
    [
        ("server", "clm-renew.timer", ["clmd.socket", "clmd.service", "clm-renew.service"]),
        ("client", "clm-client-update.timer", ["clm-client-update.service"]),
    ],
)
def test_installers_link_units_from_the_application_and_enable_the_timer(
    tmp_path, role, timer, units
):
    root = tmp_path / "opt"
    system_units = tmp_path / "etc"
    root.mkdir()
    system_units.mkdir()
    (system_units / timer).write_text("previous directly installed unit\n")
    override = system_units / (timer + ".d")
    override.mkdir()
    (override / "override.conf").write_text("[Timer]\nRandomizedDelaySec=0\n")
    log = tmp_path / "systemctl.log"
    command = [
        "bash",
        "-euc",
        f"""source "$1"
log=$5
systemctl() {{ printf '%s\\n' "$*" >> "$log"; }}
install_{role}_units "$2" "$3" "$4"
""",
        "test",
        str(ROOT / "scripts" / INSTALLERS[role]),
        str(ROOT),
        str(root),
        str(system_units),
        str(log),
    ]
    for _ in range(2):
        subprocess.run(command, check=True)
        for name in [timer, *units]:
            unit = root / "systemd" / name
            assert unit.is_file() and not unit.is_symlink()
            assert unit.read_bytes() == (ROOT / "packaging/systemd" / name).read_bytes()
            link = system_units / name
            assert link.is_symlink() and link.readlink() == unit
    assert (override / "override.conf").read_text() == "[Timer]\nRandomizedDelaySec=0\n"
    assert log.read_text().splitlines() == [
        "daemon-reload",
        f"enable --now {timer}",
        "daemon-reload",
        f"enable --now {timer}",
    ]


def test_client_migrates_the_legacy_hook_without_reenabling_it(tmp_path):
    config = tmp_path / "config"
    hooks = config / "hooks.d"
    hooks.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    hook = hooks / "restart-web-servers"
    hook.write_text(
        '#!/bin/sh\nset -eu\nexec "${CLM_CLIENT_ROOT:-/opt/clm-client}/bin/restart-web-servers"\n'
    )
    hook.chmod(0o600)
    legacy = bin_dir / hook.name
    legacy.write_text("#!/bin/sh\n# Custom legacy script\nexit 0\n")

    subprocess.run(
        [
            "bash",
            "-euc",
            'source "$1"; install_client_configuration "$2" "$3"',
            "test",
            str(ROOT / "scripts/install-client-debian.sh"),
            str(ROOT),
            str(config),
        ],
        check=True,
    )
    assert hook.read_text() == "#!/bin/sh\n# Custom legacy script\nexit 0\n"
    assert hook.stat().st_mode & 0o777 == 0o600
    assert not legacy.exists()
