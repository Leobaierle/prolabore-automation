from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any
from uuid import uuid4
import base64
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509 import load_der_x509_certificate


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@dataclass
class SharePointSettings:
    tenant_id: str
    client_id: str
    cert_bytes: bytes
    cert_password: str
    site_url: str  # site "home" usado pra escopo do token (mesmo tenant/host serve outros sites)


class SharePointClient:
    """
    Cliente genérico app-only (cert JWT) pro SharePoint via REST clássico.
    O token é cacheado por host — como todos os sites da Êxito estão sob
    empresa.sharepoint.com, o mesmo token serve pro site de DCTFWEB,
    pro portaldetarefas e pros sites individuais de cada cliente.
    """

    def __init__(self, settings: SharePointSettings) -> None:
        self.settings = settings
        self._access_token: str | None = None
        self._token_expiry = 0.0

    def _load_certificate_material(self):
        private_key, certificate, _ = pkcs12.load_key_and_certificates(
            self.settings.cert_bytes, self.settings.cert_password.encode("utf-8")
        )
        cert_der = certificate.public_bytes(serialization.Encoding.DER)
        thumbprint = load_der_x509_certificate(cert_der).fingerprint(hashes.SHA1())
        return private_key, b64url(thumbprint)

    def _build_client_assertion(self) -> str:
        private_key, thumbprint = self._load_certificate_material()
        now = int(time.time())
        header = {"alg": "RS256", "typ": "JWT", "x5t": thumbprint}
        payload = {
            "aud": f"https://login.microsoftonline.com/{self.settings.tenant_id}/oauth2/v2.0/token",
            "iss": self.settings.client_id,
            "sub": self.settings.client_id,
            "jti": str(uuid4()),
            "nbf": now - 60,
            "exp": now + 600,
        }
        signing_input = ".".join(
            (
                b64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
                b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
            )
        ).encode("ascii")
        signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return f"{signing_input.decode('ascii')}.{b64url(signature)}"

    def access_token(self) -> str:
        now = time.time()
        if self._access_token and now < self._token_expiry - 60:
            return self._access_token
        assertion = self._build_client_assertion()
        site_host = urllib.parse.urlparse(self.settings.site_url).netloc
        token_url = f"https://login.microsoftonline.com/{self.settings.tenant_id}/oauth2/v2.0/token"
        form = urllib.parse.urlencode(
            {
                "client_id": self.settings.client_id,
                "scope": f"https://{site_host}/.default",
                "grant_type": "client_credentials",
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": assertion,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            token_url, data=form, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        self._access_token = payload["access_token"]
        self._token_expiry = now + int(payload.get("expires_in", 3600))
        return self._access_token

    def rest_json(
        self,
        site_url: str,
        api_path: str,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        token = self.access_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json;odata=verbose"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json;odata=verbose"
            body = json.dumps(payload).encode("utf-8")
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(f"{site_url}{api_path}", data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"SharePoint {method} {api_path} falhou (HTTP {exc.code}): {error_body[:600]}")

    def folder_exists(self, folder_server_relative_url: str, site_url: str | None = None) -> bool:
        site_url = site_url or self.settings.site_url
        enc = urllib.parse.quote(folder_server_relative_url, safe="/")
        try:
            self.rest_json(site_url, f"/_api/web/GetFolderByServerRelativeUrl('{enc}')")
            return True
        except RuntimeError as exc:
            if "HTTP 404" in str(exc):
                return False
            raise

    def ensure_folder(self, folder_server_relative_url: str, site_url: str | None = None) -> None:
        site_url = site_url or self.settings.site_url
        if self.folder_exists(folder_server_relative_url, site_url=site_url):
            return
        enc = urllib.parse.quote(folder_server_relative_url, safe="/")
        self.rest_json(site_url, f"/_api/web/folders/add('{enc}')", method="POST")

    def upload_file(
        self, folder_server_relative_url: str, file_name: str, content: bytes, site_url: str | None = None
    ) -> dict[str, Any]:
        site_url = site_url or self.settings.site_url
        token = self.access_token()
        folder_enc = urllib.parse.quote(folder_server_relative_url, safe="/")
        name_enc = urllib.parse.quote(file_name.replace("'", "''"), safe="")
        url = (
            f"{site_url}/_api/web/GetFolderByServerRelativeUrl('{folder_enc}')"
            f"/Files/add(url='{name_enc}',overwrite=true)"
        )
        req = urllib.request.Request(
            url,
            data=content,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json;odata=verbose",
                "Content-Type": "application/octet-stream",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))["d"]
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Falha no upload de {file_name}: HTTP {exc.code} {body[:400]}")
