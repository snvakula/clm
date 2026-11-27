import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from clm_server import dns_records
from clm_server.acme import PRODUCTION, Acme
from clm_server.route53 import Route53, Target

RECORD = "_acme-challenge.acme.example.com"
VALUE = "X" * 43


@pytest.fixture
def route(tmp_path: Path):
    config = tmp_path / "config"
    (config / "aws").mkdir(parents=True)
    (config / "route53.yaml").write_text(
        "accounts:\n  first: {credentials_profile: a}\n  second: {credentials_profile: b}\n"
        "challenge_aliases:\n  acme.example.com: {account: first, hosted_zone_id: Z1}\n"
        "  alias.example.com: {account: first, hosted_zone_id: Z2}\n"
        "  acme.example.org: {account: second, hosted_zone_id: Z3}\n"
    )
    credentials = config / "aws/credentials"
    credentials.write_text(
        "[a]\naws_access_key_id = MOCK1\naws_secret_access_key = FAKE1\n"
        "aws_session_token = SESSION1\n"
        "[b]\naws_access_key_id = MOCK2\naws_secret_access_key = FAKE2\n"
    )
    credentials.chmod(0o600)
    return Route53(config)


def test_delegation_and_multiple_aliases(route):
    records = {
        "_acme-challenge.example.com": [RECORD],
        "_acme-challenge.git.example.com": ["intermediate.example.com"],
        "intermediate.example.com": ["_acme-challenge.acme.example.org"],
        "_acme-challenge.example.net": ["_acme-challenge.alias.example.com"],
    }
    lookup = lambda name: records.get(name, [])  # noqa: E731
    assert route.discover("*.example.com", lookup) == Target("first", "acme.example.com", "Z1")
    assert route.discover("git.example.com", lookup) == Target("second", "acme.example.org", "Z3")
    assert route.discover("example.net", lookup) == Target("first", "alias.example.com", "Z2")
    with pytest.raises(ValueError, match=r"_acme-challenge.jira.example.com"):
        route.discover("jira.example.com", lookup)
    with pytest.raises(ValueError, match="known challenge alias"):
        route.discover(
            "example.com",
            lambda name: ["unknown.example.com"] if name != "unknown.example.com" else [],
        )
    with pytest.raises(ValueError, match="loop"):
        route.discover("example.com", lambda name: [name])
    with pytest.raises(ValueError, match="Ambiguous"):
        route.discover("example.com", lambda name: [RECORD, "unknown.example.com"])


def test_account_environments_directories_and_options(route, tmp_path, monkeypatch):
    (tmp_path / "acme/instances").mkdir(parents=True)
    monkeypatch.setenv("AWS_SESSION_TOKEN", "UNRELATED")
    monkeypatch.setenv("AWS_PROFILE", "ambient")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AMBIENT")
    mock = Acme(tmp_path, route, "operator@example.com")
    received = []
    monkeypatch.setattr(mock, "_run", lambda args, env, operation: received.append((args, env)))
    first = route.aliases["acme.example.com"]
    second = route.aliases["acme.example.org"]
    mock._command("issue", first, "*.example.com", "ec-256", ["--dns", "dns_aws"])
    mock._command("renew", second, "git.example.com", "ec-256", [])
    (args, env), (other_args, other_env) = received
    assert PRODUCTION in args and PRODUCTION in other_args
    assert "--ecc" in other_args and "--ecc" not in args
    assert env["AWS_ACCESS_KEY_ID"] == "MOCK1"
    assert env["AWS_SESSION_TOKEN"] == "SESSION1"
    assert other_env["AWS_ACCESS_KEY_ID"] == "MOCK2"
    assert "AWS_SESSION_TOKEN" not in other_env and "AWS_PROFILE" not in other_env
    assert env["CLM_HOSTED_ZONE"] == "Z1" and other_env["CLM_HOSTED_ZONE"] == "Z3"
    assert env["HOME"] != other_env["HOME"]
    for flag in ("--home", "--config-home", "--cert-home"):
        assert args[args.index(flag) + 1] != other_args[other_args.index(flag) + 1]
    key = mock.instance("first") / "config/account.key"
    key.write_text("MOCK ACCOUNT KEY STATE")
    assert not (mock.instance("second") / "config/account.key").exists()
    assert mock.instance("first").stat().st_mode & 0o777 == 0o700


def response(values=(), ttl=900):
    nodes = "".join(f"<ResourceRecord><Value>{value}</Value></ResourceRecord>" for value in values)
    record = (
        (
            f"<ResourceRecordSet><Name>{RECORD}.</Name><Type>TXT</Type><TTL>{ttl}</TTL>"
            f"<ResourceRecords>{nodes}</ResourceRecords></ResourceRecordSet>"
        )
        if values
        else ""
    )
    return (
        f'<ListResourceRecordSetsResponse xmlns="{dns_records.NS}"><ResourceRecordSets>'
        f"{record}</ResourceRecordSets></ListResourceRecordSetsResponse>"
    ).encode()


def entries(payload):
    tag = dns_records.tag
    return [
        (
            element.findtext(tag("Action")),
            element.findtext(f".//{tag('TTL')}"),
            [item.text for item in element.iter(tag("Value"))],
        )
        for element in ET.fromstring(payload).iter(tag("Change"))
    ]


def test_exact_conditional_updates_and_cleanup_preserve_other_values():
    records = ['"operator value"', '"unrelated value with &amp; quotes"']
    add = dns_records.change(response(records), RECORD, VALUE, remove=False)
    previous = ['"operator value"', '"unrelated value with & quotes"']
    assert entries(add) == [
        ("DELETE", "900", previous),
        ("CREATE", "900", [*previous, f'"{VALUE}"']),
    ]
    remove = dns_records.change(response([*records, f'"{VALUE}"']), RECORD, VALUE, remove=True)
    assert entries(remove) == [
        ("DELETE", "900", [*previous, f'"{VALUE}"']),
        ("CREATE", "900", previous),
    ]
    assert dns_records.change(response(records), RECORD, VALUE, remove=True) == b""
    with pytest.raises(ValueError, match="owned by another"):
        dns_records.change(response([f'"{VALUE}"']), RECORD, VALUE, remove=False)
    assert entries(dns_records.change(response(), RECORD, VALUE, remove=False)) == [
        ("CREATE", "300", [f'"{VALUE}"']),
    ]
    assert entries(dns_records.change(response([f'"{VALUE}"']), RECORD, VALUE, remove=True)) == [
        ("DELETE", "900", [f'"{VALUE}"']),
    ]


def test_zone_validation_matches_public_delegation(monkeypatch):
    answer = f'''<GetHostedZoneResponse xmlns="{dns_records.NS}">
      <HostedZone><Id>/hostedzone/Z1</Id><Name>example.com.</Name>
        <Config><PrivateZone>false</PrivateZone></Config></HostedZone>
      <DelegationSet><NameServers><NameServer>ns.example.org</NameServer></NameServers></DelegationSet>
    </GetHostedZoneResponse>'''.encode()
    monkeypatch.setattr(
        dns_records,
        "public_nameservers",
        lambda name: ["ns.example.org"] if name == "example.com" else [],
    )
    dns_records.verify_zone(answer, "Z1", RECORD, delegation=True)
    with pytest.raises(ValueError, match="unexpected zone"):
        dns_records.verify_zone(answer, "Z2", RECORD, delegation=True)
    with pytest.raises(ValueError, match="does not contain"):
        dns_records.verify_zone(answer, "Z1", "_acme-challenge.example.net", delegation=True)
    monkeypatch.setattr(dns_records, "public_nameservers", lambda name: ["ns.example.org"])
    with pytest.raises(ValueError, match="more specific delegated zone"):
        dns_records.verify_zone(answer, "Z1", RECORD, delegation=True)


def test_recover_dns_uses_original_zone_and_credentials(route, tmp_path, monkeypatch):
    (tmp_path / "acme/instances").mkdir(parents=True)
    engine = Acme(tmp_path, route, "operator@example.com")
    journal = engine.instance("first") / "dns-operation.json"
    journal.write_text(json.dumps({"record": RECORD, "zone": "Z1", "value": VALUE}))
    journal.chmod(0o600)
    calls = []
    monkeypatch.setattr(engine, "_run", lambda *args: calls.append(args))
    engine.recover_dns()
    assert calls[0][1]["AWS_ACCESS_KEY_ID"] == "MOCK1"
    assert calls[0][1]["CLM_HOSTED_ZONE"] == "Z1"
    assert VALUE not in repr(calls)
    route.aliases.clear()
    with pytest.raises(ValueError, match="original Route 53 configuration"):
        engine.recover_dns()
