import pytest

from client_linux import identity as client_identity
from server import identity as server_identity

pytestmark = pytest.mark.parametrize("identity", [server_identity, client_identity])


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("EXAMPLE.COM.", "example.com"),
        ("*.Example.COM", "*.example.com"),
        ("bücher.example", "xn--bcher-kva.example"),
    ],
)
def test_normalisation(identity, source, expected):
    assert identity.normalise_identifier(source) == expected


@pytest.mark.parametrize("source", ["", " example.com", "a.*.example.com", "*", "127.0.0.1"])
def test_invalid(identity, source):
    with pytest.raises(ValueError):
        identity.normalise_identifier(source)


def test_distinct_identifiers(identity):
    assert identity.storage_id("*.example.com") != identity.storage_id("wildcard.example.com")
