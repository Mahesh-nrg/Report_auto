"""Self-signed certificate for serving the gateway over HTTPS on a LAN."""

from __future__ import annotations

import datetime
import ipaddress
import os
import socket
from pathlib import Path

VALID_DAYS = 825


class TlsUnavailable(RuntimeError):
    pass


def _subject_alt_names(x509: object) -> list[object]:
    names: list[object] = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]  # type: ignore[attr-defined]
    hostname = socket.gethostname()
    if hostname and hostname != "localhost":
        names.append(x509.DNSName(hostname))  # type: ignore[attr-defined]
    return names


def ensure_self_signed(data_dir: str | os.PathLike[str]) -> tuple[str, str]:
    """Return ``(certfile, keyfile)`` under ``<data_dir>/tls``, creating them if missing."""
    tls_dir = Path(data_dir) / "tls"
    cert, key = tls_dir / "gateway.crt", tls_dir / "gateway.key"
    if cert.exists() and key.exists():
        return str(cert), str(key)
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as exc:
        raise TlsUnavailable(
            "HTTPS needs the 'cryptography' package: pip install 'pwndoc-ai[https]'"
        ) from exc

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pwndoc-ai gateway")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(_subject_alt_names(x509)), critical=False)
        .sign(private_key, hashes.SHA256())
    )
    tls_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(
            private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return str(cert), str(key)
