#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zipfile import ZipFile
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sharepoint_client import (
    SharePointClient,
    SharePointSettings,
    build_create_payload,
    build_finalize_payload,
    create_task_item,
    fetch_task_for_finalize,
    fetch_uploaded_item,
    resolve_stage_assignee_id,
    update_file_metadata,
    update_task_item,
)


PROCESSO_ID = 9
AREA_ID = 3
COMPETENCIA = "Abril 2026"
PRAZO = "2026-05-07T00:00:00Z"
EXECUCAO_INICIO = "2026-04-23T12:00:00Z"
EXECUCAO_PRAZO = "2026-05-07T00:00:00Z"
EXECUCAO_MARGEM = "2026-05-07T00:00:00Z"
SKIP_SIGLAS = {"3PO", "ACM"}
DEFAULT_EXECUCAO_ASSIGNEE_ID = 300349


@dataclass
class EmpresaRow:
    sigla: str
    empresa: str
    processar: str
    obs: str = ""


# Empresas com "TRIMESTRAL" na coluna D so entram nas competencias destes meses.
TRIMESTRAL_MESES = {3, 6, 9, 12}

_MESES_PT = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "março": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
    "outubro": 10, "novembro": 11, "dezembro": 12,
}


def mes_de_competencia(valor: str) -> int:
    """Aceita "Setembro 2026", "09 2026", "09/2026", "9" -> 9. Erro se nao reconhecer."""
    token = re.sub(r"\s+", " ", (valor or "").strip()).split(" ")[0].strip("/")
    if token.isdigit():
        return int(token)
    mes = _MESES_PT.get(token.casefold())
    if mes is None:
        raise ValueError(f"Nao consegui extrair o mes de competencia de {valor!r}")
    return mes


def aplicar_trimestral(empresas: list["EmpresaRow"], competencia_mes: int) -> list["EmpresaRow"]:
    """Remove empresas marcadas TRIMESTRAL quando o mes nao e 03/06/09/12."""
    if competencia_mes in TRIMESTRAL_MESES:
        return empresas
    return [e for e in empresas if "TRIMESTRAL" not in (e.obs or "").upper()]


def empresas_trimestrais(planilha: Path) -> set[str]:
    """Nomes normalizados das empresas marcadas TRIMESTRAL (p/ filtrar --from-folder)."""
    return {
        normalize_name(e.empresa)
        for e in load_empresas(planilha)
        if "TRIMESTRAL" in (e.obs or "").upper()
    }


def normalize_name(value: str) -> str:
    collapsed = re.sub(r"\s+", " ", value or "").strip().casefold()
    return collapsed


def load_empresas(planilha: Path) -> list[EmpresaRow]:
    with ZipFile(planilha) as arquivo_xlsx:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in arquivo_xlsx.namelist():
            root = ET.fromstring(arquivo_xlsx.read("xl/sharedStrings.xml"))
            ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            for item in root.findall("a:si", ns):
                texto = "".join(parte.text or "" for parte in item.findall(".//a:t", ns))
                shared_strings.append(texto)

        root = ET.fromstring(arquivo_xlsx.read("xl/worksheets/sheet1.xml"))
        ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        empresas: list[EmpresaRow] = []
        for row in root.findall(".//a:sheetData/a:row", ns):
            values: dict[str, str] = {}
            for coluna in row.findall("a:c", ns):
                referencia = coluna.get("r", "")
                letra_coluna = "".join(caractere for caractere in referencia if caractere.isalpha())
                tipo = coluna.get("t")
                if tipo in ("inlineStr", "str"):
                    # <c t="inlineStr"><is><t>...</t></is></c>  (openpyxl)  ou  <c t="str"><v>...</v></c>
                    partes = coluna.findall(".//a:t", ns) or coluna.findall("a:v", ns)
                    texto = "".join(parte.text or "" for parte in partes)
                else:
                    valor = coluna.find("a:v", ns)
                    texto = "" if valor is None else valor.text or ""
                    if tipo == "s" and texto:
                        texto = shared_strings[int(texto)]
                values[letra_coluna] = texto
            sigla = str(values.get("A", "")).strip()
            empresa = str(values.get("B", "")).strip()
            processar = str(values.get("C", "")).strip().upper()
            # coluna D = CNPJ (usada pelo robô DCTFWeb); coluna E = observações (ex. TRIMESTRAL)
            obs = str(values.get("E", "")).strip()
            if sigla and empresa and processar == "SIM":
                empresas.append(EmpresaRow(sigla=sigla, empresa=empresa, processar=processar, obs=obs))
    return empresas


def fetch_all_clients(client: SharePointClient) -> list[dict[str, Any]]:
    api_path = (
        "/_api/web/lists/getbytitle('Clientes')/items?"
        "$select=ID,Title,SiteCliente,BibliotecaPublicacao,PastaPublicacao&$top=5000"
    )
    response = client.rest_json(client.settings.task_site_url, api_path)
    return response["d"]["results"]


def build_client_index(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        index.setdefault(normalize_name(str(row.get("Title") or "")), []).append(row)
    return index


def resolve_client_row(index: dict[str, list[dict[str, Any]]], empresa: str) -> dict[str, Any]:
    normalized = normalize_name(empresa)
    exact = index.get(normalized, [])
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise RuntimeError(f"Mais de um cliente SharePoint encontrado para '{empresa}'.")

    matches = []
    for title_key, rows in index.items():
        if normalized in title_key or title_key in normalized:
            matches.extend(rows)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise RuntimeError(f"Cliente SharePoint não encontrado para '{empresa}'.")
    raise RuntimeError(f"Cliente SharePoint ambíguo para '{empresa}': {[row.get('Title') for row in matches]}")


def fetch_existing_tasks(client: SharePointClient, cliente_id: int) -> list[dict[str, Any]]:
    params = {
        "$filter": f"ClienteId eq {cliente_id} and ProcessoId eq {PROCESSO_ID} and Competencia eq %27{COMPETENCIA}%27",
        "$select": "ID,Title,PassoAtual,UsuarioAtualId,ExecucaoUserAprovId",
        "$top": "20",
    }
    query = urllib.parse.urlencode(params, safe="%(),/$")
    response = client.rest_json(
        client.settings.task_site_url,
        f"/_api/web/lists/getbytitle(%27Tarefas%27)/items?{query}",
    )
    return response["d"]["results"]


def resolve_library_for_site(client: SharePointClient, client_site_url: str, expected_title: str) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {
            "$select": "Id,Title,ListItemEntityTypeFullName,BaseTemplate,RootFolder/ServerRelativeUrl,RootFolder/Name",
            "$expand": "RootFolder",
        }
    )
    response = client.rest_json(client_site_url, f"/_api/web/lists?{query}")
    matches = [
        row
        for row in response["d"]["results"]
        if row.get("BaseTemplate") == 101 and row.get("Title") == expected_title
    ]
    if not matches:
        raise RuntimeError(f"Nenhuma biblioteca encontrada com título '{expected_title}' em {client_site_url}.")
    if len(matches) > 1:
        paths = ", ".join(match["RootFolder"]["ServerRelativeUrl"] for match in matches)
        raise RuntimeError(f"Mais de uma biblioteca encontrada com título '{expected_title}': {paths}")
    return matches[0]


def fetch_uploaded_item_with_retry(
    client: SharePointClient,
    client_site_url: str,
    file_server_relative_url: str,
    attempts: int = 5,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return fetch_uploaded_item(client, client_site_url, file_server_relative_url)
        except Exception as exc:
            last_error = exc
            if "HTTP 409" not in str(exc) or attempt == attempts - 1:
                raise
            time.sleep(1 + attempt)
    if last_error is not None:
        raise last_error
    raise RuntimeError("Falha inesperada ao recuperar metadados do upload.")


def upload_for_task(
    client: SharePointClient,
    task_id: int,
    task: dict[str, Any],
    client_row: dict[str, Any],
    file_path: Path,
) -> dict[str, Any]:
    client_site = client_row.get("SiteCliente") or {}
    client_site_url = str(client_site.get("Url") or "").rstrip("/")
    if not client_site_url:
        raise RuntimeError(f"Cliente sem SiteCliente configurado para '{client_row.get('Title')}'.")

    library_title = task["Cliente"].get("BibliotecaPublicacao") or client_row.get("BibliotecaPublicacao")
    if not library_title:
        raise RuntimeError(f"Cliente sem BibliotecaPublicacao configurada para '{client_row.get('Title')}'.")
    library = resolve_library_for_site(client, client_site_url, str(library_title))
    folder_server_relative_url = library["RootFolder"]["ServerRelativeUrl"]
    publication_folder = task["Cliente"].get("PastaPublicacao") or client_row.get("PastaPublicacao")
    if publication_folder:
        folder_server_relative_url = f"{folder_server_relative_url.rstrip('/')}/{str(publication_folder).strip('/')}"

    upload_result = client.upload_file(client_site_url, folder_server_relative_url, file_path)
    file_server_relative_url = upload_result["ServerRelativeUrl"]
    uploaded_item = fetch_uploaded_item_with_retry(client, client_site_url, file_server_relative_url)
    metadata = {
        "ItemParentID": task["ID"],
        "Processo": task["Processo"]["Title"] if task.get("Processo") else None,
        "Area": task["Area"]["Title"] if task.get("Area") else None,
        "Competencia": task.get("Competencia"),
    }
    metadata = {key: value for key, value in metadata.items() if value not in (None, "")}
    update_file_metadata(
        client,
        client_site_url,
        file_server_relative_url,
        uploaded_item["__metadata"]["type"],
        metadata,
    )
    return {
        "file": str(file_path),
        "client_site_url": client_site_url,
        "library_title": library["Title"],
        "uploaded_file_server_relative_url": file_server_relative_url,
        "uploaded_item_id": uploaded_item["ID"],
    }


def build_create_args(cliente_id: int) -> argparse.Namespace:
    return argparse.Namespace(
        area_id=AREA_ID,
        processo_id=PROCESSO_ID,
        cliente_id=cliente_id,
        competencia=COMPETENCIA,
        prazo=PRAZO,
        descricao="",
        execucao_inicio=EXECUCAO_INICIO,
        execucao_prazo=EXECUCAO_PRAZO,
        execucao_margem=EXECUCAO_MARGEM,
        vencimento=None,
        vencimento_margem=None,
        usuario_atual_id=DEFAULT_EXECUCAO_ASSIGNEE_ID,
        origem="Manual",
        checklist_option=False,
        checklist_items="[]",
        notificacao_cliente="",
    )


def process_empresa(
    client: SharePointClient,
    client_index: dict[str, list[dict[str, Any]]],
    empresa_row: EmpresaRow,
    docs_dir: Path,
    dry_run: bool,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "sigla": empresa_row.sigla,
        "empresa": empresa_row.empresa,
    }
    client_row = resolve_client_row(client_index, empresa_row.empresa)
    cliente_id = int(client_row["ID"])
    result["cliente_id"] = cliente_id
    result["cliente_title"] = client_row.get("Title")

    fopag = docs_dir / f"{empresa_row.empresa} FOPAG - 04 2026.pdf"
    recpag = docs_dir / f"{empresa_row.empresa} RECPAG - 04 2026.pdf"
    missing_files = [str(path) for path in (fopag, recpag) if not path.is_file()]
    if missing_files:
        raise RuntimeError(f"Arquivos ausentes: {missing_files}")

    if dry_run:
        existing_tasks = fetch_existing_tasks(client, cliente_id)
        if existing_tasks:
            result["status"] = "skipped_existing_task"
            result["existing_tasks"] = existing_tasks
            return result
        result["status"] = "dry_run"
        result["files"] = [str(fopag), str(recpag)]
        return result

    existing_tasks = fetch_existing_tasks(client, cliente_id)
    task_id: int
    if existing_tasks:
        resumable = [row for row in existing_tasks if row.get("PassoAtual") == "Execucao"]
        if not resumable:
            result["status"] = "skipped_existing_task"
            result["existing_tasks"] = existing_tasks
            return result
        task_id = int(sorted(resumable, key=lambda row: int(row["ID"]))[-1]["ID"])
        result["resumed_task_id"] = task_id
    else:
        create_payload = build_create_payload(build_create_args(cliente_id), usuario_atual_id=DEFAULT_EXECUCAO_ASSIGNEE_ID)
        created = create_task_item(client, create_payload)
        task_id = int(created["ID"])
    result["task_id"] = task_id

    task = fetch_task_for_finalize(client, task_id)
    uploads = [
        upload_for_task(client, task_id, task, client_row, fopag),
        upload_for_task(client, task_id, task, client_row, recpag),
    ]
    result["uploads"] = uploads

    revisao_assignee_id = resolve_stage_assignee_id(client, "revisao")
    finalize_payload = build_finalize_payload(task, usuario_atual_id=revisao_assignee_id)
    update_task_item(client, task_id, task["__metadata"]["type"], finalize_payload)

    final_task = fetch_task_for_finalize(client, task_id)
    result["status"] = "completed"
    result["final_stage"] = final_task.get("PassoAtual")
    result["usuario_atual_id"] = final_task.get("UsuarioAtualId")
    result["usuario_atual"] = (final_task.get("UsuarioAtual") or {}).get("Title")
    result["execucao_user_aprov_id"] = final_task.get("ExecucaoUserAprovId")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Processa lote Prolabore 04/2026 no SharePoint.")
    parser.add_argument("--planilha", default="Empresas.xlsx")
    parser.add_argument("--docs-dir", default="documentos")
    parser.add_argument("--report", default=str(Path(__file__).resolve().parent / "reports" / "prolabore_04_2026_report.json"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = SharePointSettings.from_env()
    client = SharePointClient(settings)
    empresas = load_empresas(Path(args.planilha))
    empresas = aplicar_trimestral(empresas, mes_de_competencia(COMPETENCIA))
    client_rows = fetch_all_clients(client)
    client_index = build_client_index(client_rows)

    results: list[dict[str, Any]] = []
    report_path = Path(args.report)
    for empresa_row in empresas:
        if empresa_row.sigla in SKIP_SIGLAS:
            results.append(
                {
                    "sigla": empresa_row.sigla,
                    "empresa": empresa_row.empresa,
                    "status": "skipped_previously_processed",
                }
            )
            continue
        try:
            results.append(process_empresa(client, client_index, empresa_row, Path(args.docs_dir), args.dry_run))
        except Exception as exc:
            results.append(
                {
                    "sigla": empresa_row.sigla,
                    "empresa": empresa_row.empresa,
                    "status": "failed",
                    "error": str(exc),
                }
            )
        report_path.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    report_path.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(report_path), "results": results}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
