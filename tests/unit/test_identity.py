import pytest

from clm_common.identity import normalise_identifier, storage_id


@pytest.mark.parametrize(("source", "expected"), [
    ("EXAMPLE.COM.", "example.com"),
    ("*.Example.COM", "*.example.com"),
    ("bücher.example", "xn--bcher-kva.example"),
])
def test_normalisation(source, expected):
    assert normalise_identifier(source) == expected


@pytest.mark.parametrize("source", ["", " example.com", "a.*.example.com", "*", "127.0.0.1"])
def test_invalid(source):
    with pytest.raises(ValueError):
        normalise_identifier(source)


def test_distinct_identifiers():
    assert storage_id("*.example.com") != storage_id("wildcard.example.com")
