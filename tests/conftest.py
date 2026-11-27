from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


@pytest.fixture
def pki(tmp_path: Path):
    today = datetime.now(UTC)

    def authority(name, issuer=None, issuer_key=None):
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer.subject if issuer else subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(today - timedelta(days=2))
            .not_valid_after(today + timedelta(days=90))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(issuer_key or key, hashes.SHA256())
        )
        return certificate, key

    root, root_key = authority("CLM test trust root")
    intermediate, intermediate_key = authority("CLM test intermediate", root, root_key)
    chain = intermediate.public_bytes(serialization.Encoding.PEM)
    trust = tmp_path / "trust.pem"
    trust.write_bytes(root.public_bytes(serialization.Encoding.PEM))

    def material(identifier, *, extra=(), expired=False, key=None):
        key = key or ec.generate_private_key(ec.SECP256R1())
        leaf = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, identifier)]))
            .issuer_name(intermediate.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(today - timedelta(days=2))
            .not_valid_after(today + timedelta(days=-1 if expired else 15))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(identifier), *extra]),
                critical=False,
            )
            .sign(intermediate_key, hashes.SHA256())
        ).public_bytes(serialization.Encoding.PEM)
        return {
            "cert.pem": leaf,
            "chain.pem": chain,
            "fullchain.pem": leaf + chain,
            "privkey.pem": key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        }

    return trust, material
