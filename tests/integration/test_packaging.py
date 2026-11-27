import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REVISION = "5567d7fe7298fd37aca53194885ba444c6678669"
TARBALL_SHA256 = "22ca219a96d643910f79d2888d735a444672e5bc0179d3eb6142f0642a9682dc"


def test_scripts_and_timer_parameters():
    for path in (ROOT / "scripts").glob("*.sh"):
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
    assert "User=clm-api" in api
    assert "InaccessiblePaths=/opt/clm/acme /opt/clm/config" in api
    assert "ReadOnlyPaths=/opt/clm/data/certificates" in api
    assert "RestrictAddressFamilies=AF_UNIX" in api


def test_pinned_acme_patch_and_offline_recovery(tmp_path):
    fixture = os.environ.get("CLM_TEST_ACME_ARCHIVE")
    if fixture is None:
        pytest.skip("Set CLM_TEST_ACME_ARCHIVE to the pinned upstream source archive")
    archive = Path(fixture)
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
            "PYTHONPATH": str(ROOT / "src"),
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
        "PYTHONPATH": str(ROOT / "src"),
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
from clm_server import dns_records as dns

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
