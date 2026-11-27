"""HTTPS-only transport for the CLM distribution API."""

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .certificate import MAX_BUNDLE, MAX_FILE, parse_manifest
from .identity import storage_id


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def endpoint(value: str) -> str:
    try:
        url = urllib.parse.urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or url.path not in ("", "/", "/v1", "/v1/")
            or any(c.isspace() or ord(c) < 33 for c in value)
            or "%" in url.netloc
            or "\\" in value
            or (url.port is not None and not 1 <= url.port <= 65535)
        ):
            raise ValueError
    except ValueError:
        raise ValueError("Specify an HTTPS CLM server URL without credentials") from None
    return f"https://{url.netloc}/v1"


class Remote:
    def __init__(self, server: str, token: str, *, timeout: int = 20) -> None:
        self.server = endpoint(server)
        self.token = token
        self.timeout = timeout
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            NoRedirect(),
            urllib.request.HTTPSHandler(context=context),
        )

    def get(self, path: str, maximum: int) -> bytes:
        request = urllib.request.Request(
            self.server + path,
            headers={"Authorization": f"Bearer {self.token}"},
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                data = response.read(maximum + 1)
        except urllib.error.HTTPError as error:
            error.close()
            messages = {
                401: "CLM server rejected the client credentials",
                403: "CLM server denied access",
                404: "Certificate or generation is unavailable",
                429: "CLM server rate limited the request",
            }
            raise ValueError(messages.get(error.code, "CLM server returned an error")) from None
        except (OSError, ValueError):
            raise ValueError("Could not connect securely to the CLM server") from None
        if len(data) > maximum:
            raise ValueError("CLM server response exceeds the permitted size")
        return data

    def object(self, path: str, maximum: int = 256 * 1024) -> dict[str, Any]:
        try:
            data = json.loads(self.get(path, maximum))
        except (json.JSONDecodeError, UnicodeError, RecursionError):
            raise ValueError("CLM server returned invalid JSON") from None
        if not isinstance(data, dict):
            raise ValueError("CLM server returned an unexpected JSON object")
        return data

    def client(self) -> str:
        name = self.object("/client", 4096).get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("CLM server returned an invalid client identity")
        return name

    def available(self) -> list[dict]:
        certificates = self.object("/certificates").get("certificates")
        if not isinstance(certificates, list) or any(
            not isinstance(row, dict) for row in certificates
        ):
            raise ValueError("CLM server returned an invalid certificate list")
        return certificates

    def manifest(self, identifier: str) -> dict:
        return parse_manifest(
            self.get(f"/certificates/{storage_id(identifier)}/manifest", MAX_FILE),
            identifier,
        )

    def bundle(self, identifier: str, number: int) -> bytes:
        return self.get(
            f"/certificates/{storage_id(identifier)}/versions/{number}/bundle", MAX_BUNDLE
        )
