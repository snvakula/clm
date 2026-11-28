"""Construct conditional Route 53 changes for the patched dns_aws plugin."""

import hashlib
import hmac
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from .certificate import json_bytes
from .filesystem import read_regular, sync_directory, write_atomic

from .route53 import ZONE, NoRedirect, dns_name, public_nameservers

NS = "https://route53.amazonaws.com/doc/2013-04-01/"
ET.register_namespace("", NS)


def tag(name: str) -> str:
    return f"{{{NS}}}{name}"


def verify_zone(xml: bytes, zone: str, record: str, *, delegation: bool) -> None:
    hosted = ET.fromstring(xml).find(tag("HostedZone"))
    if hosted is None or hosted.findtext(tag("Id")) != f"/hostedzone/{zone}":
        raise ValueError("Route 53 returned an unexpected zone")
    name = dns_name(hosted.findtext(tag("Name"), ""))
    if hosted.findtext(f"{tag('Config')}/{tag('PrivateZone')}") != "false" or not (
        record == name or record.endswith("." + name)
    ):
        raise ValueError("Configured hosted zone is not public or does not contain the endpoint")

    if delegation:
        response = ET.fromstring(xml)
        nameservers = {
            dns_name(element.text or "")
            for element in response.findall(
                f"{tag('DelegationSet')}/{tag('NameServers')}/{tag('NameServer')}",
            )
        }
        if not nameservers or nameservers != set(public_nameservers(name)):
            raise ValueError("Configured hosted zone is not the publicly delegated zone")
        origin = record
        while origin != name:
            if public_nameservers(origin):
                raise ValueError("Challenge endpoint is in a more specific delegated zone")
            origin = origin.split(".", 1)[1]


def change(xml: bytes, record: str, value: str, *, remove: bool) -> bytes:
    response = ET.fromstring(xml)
    if response.tag != tag("ListResourceRecordSetsResponse"):
        raise ValueError("Unexpected Route 53 response")
    records = response.findall(f"{tag('ResourceRecordSets')}/{tag('ResourceRecordSet')}")
    matching = [
        item
        for item in records
        if item.findtext(tag("Name")) == record + "." and item.findtext(tag("Type")) == "TXT"
    ]
    if len(matching) > 1:
        raise ValueError("Multiple TXT record sets exist for the challenge endpoint")
    existing = matching[0] if matching else None
    ttl = "300"
    values = []
    if existing is not None:
        if {child.tag for child in existing} != {
            tag("Name"),
            tag("Type"),
            tag("TTL"),
            tag("ResourceRecords"),
        }:
            raise ValueError("Challenge endpoint must use a simple TXT record set")
        ttl = existing.findtext(tag("TTL"), "")
        values = [
            element.text
            for element in existing.findall(
                f"{tag('ResourceRecords')}/{tag('ResourceRecord')}/{tag('Value')}"
            )
        ]
        if not ttl.isdecimal() or not values or None in values:
            raise ValueError("Invalid challenge record set")
    quoted = '"' + value + '"'
    if remove and quoted not in values:
        return b""
    if not remove and any(item.strip('"') == value for item in values):
        raise ValueError("Challenge value already exists and is owned by another operation")
    updated = [item for item in values if item != quoted] if remove else [*values, quoted]
    request = ET.Element(tag("ChangeResourceRecordSetsRequest"))
    changes = ET.SubElement(ET.SubElement(request, tag("ChangeBatch")), tag("Changes"))
    if existing is not None:
        delete = ET.SubElement(changes, tag("Change"))
        ET.SubElement(delete, tag("Action")).text = "DELETE"
        delete.append(existing)
    if updated:
        create = ET.SubElement(changes, tag("Change"))
        ET.SubElement(create, tag("Action")).text = "CREATE"
        replacement = ET.SubElement(create, tag("ResourceRecordSet"))
        for key, text in (("Name", record + "."), ("Type", "TXT"), ("TTL", ttl)):
            ET.SubElement(replacement, tag(key)).text = text
        resource_records = ET.SubElement(replacement, tag("ResourceRecords"))
        for item in updated:
            ET.SubElement(
                ET.SubElement(resource_records, tag("ResourceRecord")), tag("Value")
            ).text = item
    return ET.tostring(request)


def entry(path: Path) -> dict[str, str]:
    value = json.loads(read_regular(path, 4096, private=True))
    if (
        not isinstance(value, dict)
        or set(value) != {"record", "zone", "value"}
        or not all(isinstance(item, str) for item in value.values())
        or dns_name(value["record"]) != value["record"]
        or not value["record"].startswith("_acme-challenge.")
        or not ZONE.fullmatch(value["zone"])
        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value["value"])
    ):
        raise ValueError("Invalid DNS recovery journal")
    return value


def signed_request(method: str, path: str, query: str, data: bytes) -> bytes:
    """Sign Route 53's documented SigV4 request using HMAC-SHA256."""
    host = "route53.global.api.aws"
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    day = timestamp[:8]
    scope = f"{day}/us-east-1/route53/aws4_request"
    headers = {"host": host, "x-amz-date": timestamp}
    if token := os.environ.get("AWS_SESSION_TOKEN"):
        headers["x-amz-security-token"] = token
    names = ";".join(sorted(headers))
    canonical = "\n".join(
        (
            method,
            "/" + path,
            query,
            "".join(f"{name}:{headers[name]}\n" for name in sorted(headers)),
            names,
            hashlib.sha256(data).hexdigest(),
        )
    )
    text = "\n".join(
        (
            "AWS4-HMAC-SHA256",
            timestamp,
            scope,
            hashlib.sha256(canonical.encode()).hexdigest(),
        )
    )
    key = ("AWS4" + os.environ["AWS_SECRET_ACCESS_KEY"]).encode()
    for part in scope.split("/"):
        key = hmac.digest(key, part.encode(), "sha256")
    signature = hmac.digest(key, text.encode(), "sha256").hex()
    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={os.environ['AWS_ACCESS_KEY_ID']}/{scope}, "
        f"SignedHeaders={names}, Signature={signature}"
    )
    request = urllib.request.Request(
        f"https://{host}/{path}" + (f"?{query}" if query else ""),
        data=data if method == "POST" else None,
        headers=headers,
        method=method,
    )
    if method == "POST":
        request.add_header("Content-Type", "application/xml")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=30) as response:
            return response.read(512 * 1024 + 1)
    except urllib.error.HTTPError as error:
        with error:
            return error.read(512 * 1024 + 1)


def main() -> int:
    action = sys.argv[1]
    path = Path(os.environ["CLM_DNS_JOURNAL"])
    try:
        if action == "request":
            method, request_path, query = sys.argv[2:5]
            data = sys.stdin.buffer.read(512 * 1024 + 1)
            if (
                method not in {"GET", "POST"}
                or not re.fullmatch(r"2013-04-01/hostedzone/Z[A-Z0-9]+(?:/rrset/?)?", request_path)
                or not re.fullmatch(r"(?:maxitems=1&name=[a-z0-9_.-]+&type=TXT)?", query)
                or len(data) > 512 * 1024
            ):
                raise ValueError("Invalid Route 53 request")
            payload = signed_request(method, request_path, query, data)
            if len(payload) > 512 * 1024:
                raise ValueError("Route 53 response exceeds the permitted size")
            sys.stdout.buffer.write(payload)
            return 0
        if action == "pending":
            if path.exists():
                print(entry(path)["value"])
            return 0
        record = os.environ["CLM_CHALLENGE_RECORD"]
        zone = os.environ["CLM_HOSTED_ZONE"]
        if not ZONE.fullmatch(zone) or dns_name(record) != record:
            raise ValueError("Invalid challenge endpoint")
        if action == "clear":
            path.unlink(missing_ok=True)
            sync_directory(path.parent)
            return 0
        xml = sys.stdin.buffer.read(512 * 1024 + 1)
        if len(xml) > 512 * 1024 or b"<!DOCTYPE" in xml.upper():
            raise ValueError("Invalid Route 53 XML")
        if action in ("zone", "cleanup-zone"):
            verify_zone(xml, zone, record, delegation=action == "zone")
            return 0
        value = os.environ["CLM_TXT_VALUE"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
            raise ValueError("Invalid challenge value")
        saved = {"record": record, "zone": zone, "value": value}
        if action == "remove" and (not path.exists() or entry(path) != saved):
            return 0
        result = change(xml, record, value, remove=action == "remove")
        if action == "add":
            if path.exists():
                raise ValueError("A previous DNS operation requires cleanup")
            write_atomic(path, json_bytes(saved))
        sys.stdout.buffer.write(result)
        return 0
    except (ET.ParseError, OSError, ValueError, KeyError):
        print("CLM Route 53 validation or recovery failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
