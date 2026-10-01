"""Verify HTTPS against the operating system's trusted certificates.

On machines where antivirus or a proxy inspects HTTPS, the inspecting root
certificate lives only in the Windows certificate store, so certifi-based
verification fails everywhere (yt-dlp, Hugging Face downloads). This keeps verification on and just trusts what Windows trusts:

- Python's ssl module (requests, httpx, urllib) goes through truststore.
- libcurl (curl_cffi, which yt-dlp uses to impersonate a browser for TikTok)
  can't use truststore, so on Windows it gets a PEM bundle of certifi plus the
  Windows root store via CURL_CA_BUNDLE, unless a CA bundle is already configured.
"""

import os
import ssl
import sys
import tempfile
from pathlib import Path

import truststore

SERVER_AUTH_OID = "1.3.6.1.5.5.7.3.1"
CA_ENV_VARS = ("CURL_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")


def configure() -> None:
    truststore.inject_into_ssl()
    if sys.platform == "win32" and not any(os.environ.get(v) for v in CA_ENV_VARS):
        try:
            os.environ["CURL_CA_BUNDLE"] = str(_write_windows_bundle())
        except Exception:  # never block startup over this; curl then falls back to certifi
            pass


def _write_windows_bundle() -> Path:
    import certifi

    pems = [Path(certifi.where()).read_text(encoding="ascii")]
    for store in ("ROOT", "CA"):
        for cert, encoding, trust in ssl.enum_certificates(store):
            if encoding == "x509_asn" and (trust is True or SERVER_AUTH_OID in trust):
                pems.append(ssl.DER_cert_to_PEM_cert(cert))
    path = Path(tempfile.gettempdir()) / "tiktok_archive_ca_bundle.pem"
    path.write_text("".join(pems), encoding="ascii")
    return path
