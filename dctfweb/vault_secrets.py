from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import hvac
from minio import Minio

# Segredos: senha do certificado no Vault (mount certificates/, kv-v2),
# binario .pfx no MinIO (bucket certificates), config de app no Vault
# (mount secret/, kv-v2). Autenticacao via AppRole dctfweb-worker, restrito
# por policy dctfweb-policy a esses tres caminhos.


@dataclass
class Certificado:
    pfx_bytes: bytes
    password: str


class VaultSecrets:
    def __init__(self, bootstrap: dict) -> None:
        self._vault_addr = bootstrap["VAULT_ADDR"]
        self._role_id = bootstrap["VAULT_ROLE_ID"]
        self._secret_id = bootstrap["VAULT_SECRET_ID"]
        self._minio_endpoint = bootstrap["MINIO_ENDPOINT"]
        self._minio_access_key = bootstrap["MINIO_ACCESS_KEY"]
        self._minio_secret_key = bootstrap["MINIO_SECRET_KEY"]

        self._lock = threading.Lock()
        self._client: hvac.Client | None = None
        self._token_expiry = 0.0
        self._minio: Minio | None = None

        self._cert_cache: dict[str, Certificado] = {}
        self._config_cache: dict | None = None

    def _vault_client(self) -> hvac.Client:
        with self._lock:
            now = time.time()
            if self._client is None or now > self._token_expiry - 60:
                client = hvac.Client(url=self._vault_addr)
                client.auth.approle.login(role_id=self._role_id, secret_id=self._secret_id)
                self._client = client
                self._token_expiry = now + client.auth.token.lookup_self()["data"]["ttl"]
            return self._client

    def _minio_client(self) -> Minio:
        if self._minio is None:
            self._minio = Minio(
                self._minio_endpoint,
                access_key=self._minio_access_key,
                secret_key=self._minio_secret_key,
                secure=False,
            )
        return self._minio

    def get_config(self) -> dict:
        if self._config_cache is None:
            secret = self._vault_client().secrets.kv.v2.read_secret_version(
                path="dctfweb", mount_point="secret", raise_on_deleted_version=True
            )
            self._config_cache = secret["data"]["data"]
        return self._config_cache

    def get_certificado(self, vault_secret_name: str) -> Certificado:
        if vault_secret_name in self._cert_cache:
            return self._cert_cache[vault_secret_name]

        secret = self._vault_client().secrets.kv.v2.read_secret_version(
            path=vault_secret_name, mount_point="certificates", raise_on_deleted_version=True
        )
        data = secret["data"]["data"]

        resp = self._minio_client().get_object(data["object_bucket"], data["object_path"])
        try:
            pfx_bytes = resp.read()
        finally:
            resp.close()
            resp.release_conn()

        cert = Certificado(pfx_bytes=pfx_bytes, password=data["password"])
        self._cert_cache[vault_secret_name] = cert
        return cert


_instance: VaultSecrets | None = None


def get_vault_secrets(bootstrap: dict | None = None) -> VaultSecrets:
    global _instance
    if _instance is None:
        if bootstrap is None:
            raise RuntimeError("get_vault_secrets() precisa de bootstrap na primeira chamada")
        _instance = VaultSecrets(bootstrap)
    return _instance
