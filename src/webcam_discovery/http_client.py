from __future__ import annotations

import os
import ssl

import certifi
import httpx


def get_ca_bundle() -> str:
    return (
        os.environ.get("WCD_CA_BUNDLE")
        or os.environ.get("SSL_CERT_FILE")
        or os.environ.get("REQUESTS_CA_BUNDLE")
        or certifi.where()
    )


def allow_insecure_ssl_fallback() -> bool:
    return os.environ.get("WCD_ALLOW_INSECURE_SSL_FALLBACK", "").lower() in {
        "1",
        "true",
        "yes",
        "y",
    }


def is_ssl_cert_failure(exc: BaseException) -> bool:
    text = repr(exc)
    return (
        "CERTIFICATE_VERIFY_FAILED" in text
        or "unable to get local issuer certificate" in text
        or isinstance(exc, ssl.SSLCertVerificationError)
    )


def build_async_client(**kwargs) -> httpx.AsyncClient:
    kwargs.setdefault("verify", get_ca_bundle())
    kwargs.setdefault("trust_env", True)
    return httpx.AsyncClient(**kwargs)
