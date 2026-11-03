"""Cliente SharePoint enxuto para a automação de pró-labore.

Extraído de `Sharepoint-API/sharepoint_task_attachment.py` (rotina folha_mensal) —
só o necessário para: autenticar por certificado, ler/criar/atualizar tarefas do
processo "FPA - Rotina Mensal Folha" e anexar PDFs na biblioteca do cliente.

Se o original evoluir, sincronizar manualmente as funções abaixo.
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509 import load_der_x509_certificate


JSON_ACCEPT = "application/json;odata=verbose"
HTTP_RETRYABLE_ERRNOS = {-3}
HTTP_MAX_ATTEMPTS = max(1, int(os.getenv("SHAREPOINT_HTTP_MAX_ATTEMPTS", "4")))
HTTP_RETRY_BASE_DELAY_SECONDS = max(0.0, float(os.getenv("SHAREPOINT_HTTP_RETRY_BASE_DELAY", "1.5")))


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def iso_now() -> int:
    return int(time.time())


def iso_utc_now_string() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def is_retryable_url_error(exc: urllib.error.URLError) -> bool:
    reason = exc.reason
    if isinstance(reason, socket.gaierror):
        return reason.errno in HTTP_RETRYABLE_ERRNOS
    if isinstance(reason, TimeoutError):
        return True
    if isinstance(reason, socket.timeout):
        return True
    return False


def make_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    last_error: Exception | None = None
    for attempt in range(1, HTTP_MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, dict(response.headers.items()), response.read()
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            message = payload.decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {url} falhou com HTTP {exc.code}: {message}") from exc
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt >= HTTP_MAX_ATTEMPTS or not is_retryable_url_error(exc):
                break
            time.sleep(HTTP_RETRY_BASE_DELAY_SECONDS * attempt)
    if last_error is not None:
        raise RuntimeError(
            f"{method} {url} falhou após {HTTP_MAX_ATTEMPTS} tentativas: {last_error}"
        ) from last_error
    raise RuntimeError(f"{method} {url} falhou por motivo desconhecido.")


def make_json_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request_headers = {"Accept": JSON_ACCEPT, **(headers or {})}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json;odata=verbose")
    _, _, response_body = make_request(url, method=method, headers=request_headers, body=body)
    if not response_body.strip():
        return {}
    raw = response_body.decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        snippet = raw[:600].replace("\n", "\\n")
        raise RuntimeError(f"Resposta não JSON em {method} {url}: {snippet}") from exc


def quote_odata(value: str) -> str:
    return value.replace("'", "''")


def quote_odata_url_value(value: str) -> str:
    return urllib.parse.quote(quote_odata(value), safe="/()'")


def site_server_relative_url(site_url: str) -> str:
    parsed = urllib.parse.urlparse(site_url)
    return parsed.path.rstrip("/")


def first_result(payload: dict[str, Any]) -> dict[str, Any]:
    results = payload["d"]["results"]
    if not results:
        raise RuntimeError("Nenhum resultado retornado.")
    return results[0]


@dataclass
class SharePointSettings:
    tenant_id: str
    client_id: str
    cert_pfx_path: Path
    cert_password: str
    task_site_url: str
    task_list_guid: str

    @classmethod
    def from_env(cls) -> "SharePointSettings":
        missing = [
            name
            for name in (
                "SHAREPOINT_TENANT_ID",
                "SHAREPOINT_CLIENT_ID",
                "SHAREPOINT_CERT_PFX_PATH",
                "SHAREPOINT_CERT_PASSWORD",
                "SHAREPOINT_SITE_URL",
                "SHAREPOINT_TASK_LIST_GUID",
            )
            if not os.getenv(name)
        ]
        if missing:
            raise RuntimeError(f"Variáveis obrigatórias ausentes: {', '.join(missing)}")
        return cls(
            tenant_id=os.environ["SHAREPOINT_TENANT_ID"].strip(),
            client_id=os.environ["SHAREPOINT_CLIENT_ID"].strip(),
            cert_pfx_path=Path(os.environ["SHAREPOINT_CERT_PFX_PATH"]).expanduser(),
            cert_password=os.environ["SHAREPOINT_CERT_PASSWORD"],
            task_site_url=os.environ["SHAREPOINT_SITE_URL"].strip().rstrip("/"),
            task_list_guid=os.environ["SHAREPOINT_TASK_LIST_GUID"].strip(),
        )


class SharePointClient:
    def __init__(self, settings: SharePointSettings) -> None:
        self.settings = settings
        self._access_token: str | None = None
        self._token_expiry = 0

    def _load_certificate_material(self) -> tuple[Any, str]:
        pfx_data = self.settings.cert_pfx_path.read_bytes()
        private_key, certificate, _ = pkcs12.load_key_and_certificates(
            pfx_data,
            self.settings.cert_password.encode("utf-8"),
        )
        if private_key is None or certificate is None:
            raise RuntimeError("Não foi possível carregar chave privada e certificado do PFX.")
        cert_der = certificate.public_bytes(serialization.Encoding.DER)
        thumbprint = load_der_x509_certificate(cert_der).fingerprint(hashes.SHA1())
        return private_key, b64url(thumbprint)

    def _build_client_assertion(self) -> str:
        private_key, thumbprint = self._load_certificate_material()
        now = iso_now()
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

    def access_token(self, resource_site_url: str) -> str:
        now = iso_now()
        if self._access_token and now < self._token_expiry - 60:
            return self._access_token

        assertion = self._build_client_assertion()
        site_host = urllib.parse.urlparse(resource_site_url).netloc
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
        _, _, response_body = make_request(
            token_url,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=form,
        )
        payload = json.loads(response_body.decode("utf-8"))
        self._access_token = payload["access_token"]
        self._token_expiry = now + int(payload.get("expires_in", 3600))
        return self._access_token

    def rest_json(
        self,
        site_url: str,
        api_path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        token = self.access_token(site_url)
        base_headers = {"Authorization": f"Bearer {token}", "Accept": JSON_ACCEPT}
        if payload is not None:
            base_headers["Content-Type"] = "application/json;odata=verbose"
        if headers:
            base_headers.update(headers)
        data = payload if payload is not None else None
        return make_json_request(f"{site_url}{api_path}", method=method, headers=base_headers, payload=data)

    def upload_file(self, site_url: str, folder_server_relative_url: str, file_path: Path) -> dict[str, Any]:
        token = self.access_token(site_url)
        file_name = file_path.name.replace("'", "''")
        mime_type, _ = mimetypes.guess_type(file_path.name)
        _, _, response_body = make_request(
            f"{site_url}/_api/web/GetFolderByServerRelativeUrl('{quote_odata_url_value(folder_server_relative_url)}')/Files/add(url='{urllib.parse.quote(file_name, safe='')}',overwrite=true)",
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": JSON_ACCEPT,
                "Content-Type": mime_type or "application/octet-stream",
            },
            body=file_path.read_bytes(),
        )
        return json.loads(response_body.decode("utf-8"))["d"]


def configured_stage_assignee(stage: str) -> str | None:
    value = os.getenv(f"SHAREPOINT_{stage.upper()}_ASSIGNEE", "").strip()
    return value or None


def resolve_user_info_id(client: SharePointClient, assignee: str | int | None) -> int | None:
    if assignee is None:
        return None
    raw = str(assignee).strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)

    normalized = raw.casefold()
    user_sources = (
        ("/_api/web/siteuserinfolist/items?$select=ID,Title,EMail&$top=5000", "ID", "EMail"),
        ("/_api/web/siteusers?$select=Id,Title,Email&$top=5000", "Id", "Email"),
    )
    for api_path, id_key, email_key in user_sources:
        response = client.rest_json(client.settings.task_site_url, api_path)
        for row in response["d"]["results"]:
            title = str(row.get("Title") or "").strip()
            email = str(row.get(email_key) or "").strip()
            if normalized in {title.casefold(), email.casefold()}:
                return int(row[id_key])

    raise RuntimeError(
        f"Usuário configurado para SharePoint não foi encontrado no site: {raw}. "
        "Use o ID interno, o título exato ou o e-mail."
    )


def resolve_stage_assignee_id(
    client: SharePointClient,
    stage: str,
    explicit_assignee: int | None = None,
) -> int | None:
    if explicit_assignee is not None:
        return explicit_assignee
    configured = configured_stage_assignee(stage)
    if configured is None:
        return None
    return resolve_user_info_id(client, configured)


def fetch_task_for_finalize(client: SharePointClient, task_id: int) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {
            "$filter": f"ID eq {task_id}",
            "$select": "*,Cliente/ID,Cliente/Title,Cliente/BibliotecaPublicacao,Cliente/PastaPublicacao,Processo/ID,Processo/Title,Area/ID,Area/Title,UsuarioAtual/ID,UsuarioAtual/Title,UsuarioAtual/EMail",
            "$expand": "Cliente,Processo,Area,UsuarioAtual",
        }
    )
    api_path = f"/_api/Web/Lists(guid'{client.settings.task_list_guid}')/Items?{query}"
    return first_result(client.rest_json(client.settings.task_site_url, api_path))


def fetch_uploaded_item(
    client: SharePointClient,
    client_site_url: str,
    file_server_relative_url: str,
) -> dict[str, Any]:
    query = urllib.parse.urlencode({"$select": "ID,Id,__metadata/type"})
    api_path = f"/_api/web/GetFileByServerRelativeUrl('{quote_odata_url_value(file_server_relative_url)}')/ListItemAllFields?{query}"
    return client.rest_json(client_site_url, api_path)["d"]


def update_file_metadata(
    client: SharePointClient,
    client_site_url: str,
    file_server_relative_url: str,
    item_type: str,
    metadata: dict[str, Any],
) -> None:
    api_path = f"/_api/web/GetFileByServerRelativeUrl('{quote_odata_url_value(file_server_relative_url)}')/ListItemAllFields"
    payload = {"__metadata": {"type": item_type}, **metadata}
    client.rest_json(
        client_site_url,
        api_path,
        method="POST",
        payload=payload,
        headers={"X-HTTP-Method": "MERGE", "IF-MATCH": "*"},
    )


def update_task_item(
    client: SharePointClient,
    task_id: int,
    item_type: str,
    payload: dict[str, Any],
) -> None:
    api_path = f"/_api/web/lists/getbytitle('Tarefas')/items({task_id})"
    client.rest_json(
        client.settings.task_site_url,
        api_path,
        method="POST",
        payload={"__metadata": {"type": item_type}, **payload},
        headers={"X-HTTP-Method": "MERGE", "IF-MATCH": "*"},
    )


def create_task_item(client: SharePointClient, payload: dict[str, Any]) -> dict[str, Any]:
    api_path = "/_api/web/lists/getbytitle('Tarefas')/items"
    return client.rest_json(
        client.settings.task_site_url,
        api_path,
        method="POST",
        payload={"__metadata": {"type": "SP.Data.TarefasListItem"}, **payload},
    )["d"]


def build_finalize_payload(
    task: dict[str, Any],
    comment_html: str | None = None,
    usuario_atual_id: int | None = None,
) -> dict[str, Any]:
    now = iso_utc_now_string()
    execucao_usuario_atual_id = task.get("UsuarioAtual", {}).get("ID") or task.get("UsuarioAtualId")
    payload = {
        "Id": task["Id"],
        "ID": task["ID"],
        "Title": "Revisão e Validação",
        "Action": "Aprovar",
        "Descricao": task.get("Descricao"),
        "Competencia": task.get("Competencia"),
        "CheckListOption": task.get("CheckListOption"),
        "CheckListItems": task.get("CheckListItems"),
        "Comments": comment_html or task.get("Comments") or "",
        "PassoAnterior": "Execucao",
        "PassoAtual": "RevisaoEValidacao",
        "ExecucaoInicio": task.get("ExecucaoInicio"),
        "ExecucaoFim": now,
        "ExecucaoPrazo": task.get("ExecucaoPrazo"),
        "ExecucaoMargem": task.get("ExecucaoMargem"),
        "RevisaoEValidacaoInicio": now,
        "RevisaoEValidacaoFim": task.get("RevisaoEValidacaoFim"),
        "RevisaoEValidacaoPrazo": task.get("RevisaoEValidacaoPrazo"),
        "RevisaoEValidacaoMargem": task.get("RevisaoEValidacaoMargem"),
        "PublicacaoInicio": task.get("PublicacaoInicio"),
        "PublicacaoFim": task.get("PublicacaoFim"),
        "PublicacaoPrazo": task.get("PublicacaoPrazo"),
        "PublicacaoMargem": task.get("PublicacaoMargem"),
        "FechamentoInicio": task.get("FechamentoInicio"),
        "FechamentoFim": task.get("FechamentoFim"),
        "FechamentoPrazo": task.get("FechamentoPrazo"),
        "FechamentoMargem": task.get("FechamentoMargem"),
        "Prazo": task.get("Prazo"),
        "PendenciaCliente": task.get("PendenciaCliente"),
        "MotivoDaPendencia": task.get("MotivoDaPendencia"),
        "PrazoPendencia": task.get("PrazoPendencia"),
        "MotivoDaPendenciaHistorico": task.get("MotivoDaPendenciaHistorico"),
        "NotificacaoCliente": task.get("NotificacaoCliente") or "",
        "ExecucaoUserAprovId": execucao_usuario_atual_id,
        "UsuarioAtualId": usuario_atual_id or execucao_usuario_atual_id,
        "Vencimento": task.get("RevisaoEValidacaoPrazo") or task.get("Vencimento"),
        "VencimentoMargem": task.get("RevisaoEValidacaoMargem") or task.get("VencimentoMargem"),
    }
    return payload


def build_create_payload(args: argparse.Namespace, usuario_atual_id: int | None = None) -> dict[str, Any]:
    resolved_usuario_atual_id = usuario_atual_id or args.usuario_atual_id or 300349
    return {
        "Title": "Execução",
        "Action": "Aprovar",
        "AreaId": str(args.area_id),
        "ProcessoId": str(args.processo_id),
        "ClienteId": str(args.cliente_id),
        "Competencia": args.competencia,
        "Descricao": args.descricao,
        "Prazo": args.prazo,
        "Origem": args.origem,
        "PassoAtual": "Execucao",
        "UsuarioAtualId": resolved_usuario_atual_id,
        "ExecucaoInicio": args.execucao_inicio,
        "ExecucaoPrazo": args.execucao_prazo,
        "ExecucaoMargem": args.execucao_margem,
        "Vencimento": args.vencimento or args.execucao_prazo,
        "VencimentoMargem": args.vencimento_margem or args.execucao_margem,
        "CheckListOption": args.checklist_option,
        "CheckListItems": args.checklist_items,
        "NotificacaoCliente": args.notificacao_cliente or "",
    }
