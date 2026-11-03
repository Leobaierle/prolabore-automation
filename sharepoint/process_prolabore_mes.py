#!/usr/bin/env python3
"""Passos 4-5 do pro-labore para QUALQUER competencia: cria (ou retoma) a tarefa no
Portal, anexa FOPAG + RECPAG e avanca a etapa para Revisao e Validacao.

Mesma logica validada de process_prolabore_08_2026.py (tarefa 83384, 04/09/2026), sem o
mes fixo no codigo: a competencia vem por --competencia MM/AAAA e dela saem o texto do
Portal ("Setembro 2026"), o sufixo dos PDFs ("09 2026") e o prazo (dia 7 do mes seguinte).

Uso:
  python3 sharepoint/process_prolabore_mes.py --competencia 09/2026 [--siglas XYZ,ABC] [--dry-run]

Carrega o .env.sharepoint sozinho (raiz do repo; fallback ~/work/Sharepoint-API).
Cada empresa processada sai numa linha "@@ {json}" no stdout (lida pelo painel de automacoes).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _load_env_sharepoint() -> None:
    candidatos = [REPO_ROOT / ".env.sharepoint", Path.home() / "work" / "Sharepoint-API" / ".env.sharepoint"]
    arquivo = next((p for p in candidatos if p.is_file()), None)
    if arquivo is None:
        raise SystemExit(f".env.sharepoint nao encontrado em: {', '.join(map(str, candidatos))}")
    for line in arquivo.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


_load_env_sharepoint()

from sharepoint_client import (  # noqa: E402
    SharePointClient,
    SharePointSettings,
    build_create_payload,
    build_finalize_payload,
    create_task_item,
    fetch_task_for_finalize,
    resolve_stage_assignee_id,
    resolve_user_info_id,
    update_task_item,
)
from process_prolabore_batch import (  # noqa: E402
    EmpresaRow,
    aplicar_trimestral,
    build_client_index,
    fetch_all_clients,
    load_empresas,
    normalize_name,
    resolve_client_row,
    upload_for_task,
)

PROCESSO_ID = 9
AREA_ID = 3
DEFAULT_EXECUCAO_ASSIGNEE_ID = 343703
PRAZO_DIA = 7  # prazo = dia 7 do mes seguinte a competencia

MESES = [
    "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
]

# Prefixo do arquivo PDF -> ID do cliente no SharePoint, para casos em que o nome
# do arquivo nao bate com o Title do cliente. Chave normalizada por normalize_name.
MANUAL_CLIENT_ID: dict[str, int] = {
    "alfa representações": 101,         # ALFA SERVICOS EMPRESARIAIS LTDA
    "beta adm": 102,                    # BETA - ADMINISTRACAO E PARTICIPACOES S/A
    "clinica gama": 103,                # Clinica Gama
}


class Competencia:
    def __init__(self, valor: str) -> None:
        m = re.match(r"^(\d{2})/(\d{4})$", (valor or "").strip())
        if not m or not 1 <= int(m.group(1)) <= 12:
            raise ValueError(f"Competencia invalida: {valor!r}. Use MM/AAAA.")
        self.mes = int(m.group(1))
        self.ano = int(m.group(2))
        self.portal = f"{MESES[self.mes - 1]} {self.ano}"          # "Setembro 2026"
        self.file_tag = f"{self.mes:02d} {self.ano}"                # "09 2026"
        prox_mes, prox_ano = (1, self.ano + 1) if self.mes == 12 else (self.mes + 1, self.ano)
        self.prazo = f"{prox_ano}-{prox_mes:02d}-{PRAZO_DIA:02d}T00:00:00Z"


def emit(payload: dict[str, Any]) -> None:
    print("@@ " + json.dumps(payload, ensure_ascii=False), flush=True)


def fetch_existing_tasks(client: SharePointClient, cliente_id: int, comp: Competencia) -> list[dict[str, Any]]:
    params = {
        "$filter": f"ClienteId eq {cliente_id} and ProcessoId eq {PROCESSO_ID} and Competencia eq %27{comp.portal}%27",
        "$select": "ID,Title,PassoAtual,UsuarioAtualId,ExecucaoUserAprovId",
        "$top": "20",
    }
    query = urllib.parse.urlencode(params, safe="%(),/$")
    response = client.rest_json(
        client.settings.task_site_url,
        f"/_api/web/lists/getbytitle(%27Tarefas%27)/items?{query}",
    )
    return response["d"]["results"]


def build_create_args(comp: Competencia, cliente_id: int, execucao_assignee_id: int) -> argparse.Namespace:
    return argparse.Namespace(
        area_id=AREA_ID,
        processo_id=PROCESSO_ID,
        cliente_id=cliente_id,
        competencia=comp.portal,
        prazo=comp.prazo,
        descricao="",
        execucao_inicio=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        execucao_prazo=comp.prazo,
        execucao_margem=comp.prazo,
        vencimento=None,
        vencimento_margem=None,
        usuario_atual_id=execucao_assignee_id,
        origem="Manual",
        checklist_option=False,
        checklist_items="[]",
        # campo "Notificação cliente" do Portal (rich text) — mesmo texto das tarefas de agosto/setembro
        notificacao_cliente=(
            '<div class="ExternalClass9A1B2C3D4E5F4A6B8C7D6E5F4A3B2C1D"><p><span style="color:rgb(51, 51, 51);">'
            f"1)Folha de Pagamento {comp.mes:02d}/{comp.ano} e Recibos de Pró Labore.</span><br></p></div>"
        ),
    )


def process_empresa(
    client: SharePointClient,
    comp: Competencia,
    client_index: dict[str, list[dict[str, Any]]],
    client_by_id: dict[int, dict[str, Any]],
    empresa_row: EmpresaRow,
    docs_dir: Path,
    dry_run: bool,
    execucao_assignee_id: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {"sigla": empresa_row.sigla, "empresa": empresa_row.empresa}
    manual_id = MANUAL_CLIENT_ID.get(normalize_name(empresa_row.empresa))
    if manual_id is not None:
        client_row = client_by_id.get(manual_id)
        if client_row is None:
            raise RuntimeError(f"MANUAL_CLIENT_ID {manual_id} nao encontrado na lista de clientes.")
    else:
        client_row = resolve_client_row(client_index, empresa_row.empresa)
    cliente_id = int(client_row["ID"])
    result["cliente_id"] = cliente_id
    result["cliente_title"] = client_row.get("Title")

    fopag = docs_dir / f"{empresa_row.empresa} FOPAG - {comp.file_tag}.pdf"
    recpag = docs_dir / f"{empresa_row.empresa} RECPAG - {comp.file_tag}.pdf"
    missing_files = [p.name for p in (fopag, recpag) if not p.is_file()]
    if missing_files:
        raise RuntimeError(f"Arquivos ausentes em {docs_dir}: {missing_files}")

    existing_tasks = fetch_existing_tasks(client, cliente_id, comp)

    if dry_run:
        result["status"] = "skipped_existing_task" if existing_tasks else "dry_run"
        result["existing_tasks"] = existing_tasks
        return result

    if existing_tasks:
        resumable = [row for row in existing_tasks if row.get("PassoAtual") == "Execucao"]
        if not resumable:
            result["status"] = "skipped_existing_task"
            result["existing_tasks"] = existing_tasks
            return result
        task_id = int(sorted(resumable, key=lambda row: int(row["ID"]))[-1]["ID"])
        result["resumed_task_id"] = task_id
    else:
        create_payload = build_create_payload(
            build_create_args(comp, cliente_id, execucao_assignee_id),
            usuario_atual_id=execucao_assignee_id,
        )
        created = create_task_item(client, create_payload)
        task_id = int(created["ID"])
    result["task_id"] = task_id

    task = fetch_task_for_finalize(client, task_id)
    result["uploads"] = [
        upload_for_task(client, task_id, task, client_row, fopag),
        upload_for_task(client, task_id, task, client_row, recpag),
    ]

    revisao_assignee_id = resolve_stage_assignee_id(client, "revisao")
    finalize_payload = build_finalize_payload(task, usuario_atual_id=revisao_assignee_id)
    update_task_item(client, task_id, task["__metadata"]["type"], finalize_payload)

    final_task = fetch_task_for_finalize(client, task_id)
    result["status"] = "completed"
    result["final_stage"] = final_task.get("PassoAtual")
    result["usuario_atual"] = (final_task.get("UsuarioAtual") or {}).get("Title")
    return result


def resumo(result: dict[str, Any]) -> tuple[str, str]:
    """(status do painel, texto curto) a partir do resultado de uma empresa."""
    status = result.get("status")
    task = result.get("task_id") or result.get("resumed_task_id")
    if status == "completed":
        return "ok", f"tarefa {task} → {result.get('final_stage')} ({result.get('usuario_atual') or '—'})"
    if status == "skipped_existing_task":
        passos = ", ".join(f"{t.get('ID')}:{t.get('PassoAtual')}" for t in result.get("existing_tasks") or [])
        return "pulada", f"tarefa já existe fora da Execução ({passos})"
    if status == "dry_run":
        return "ok", "dry-run: criaria a tarefa"
    return "falha", str(result.get("error") or status)


def main() -> int:
    parser = argparse.ArgumentParser(description="Tarefas do pro-labore no Portal para uma competencia.")
    parser.add_argument("--competencia", required=True, help="MM/AAAA")
    parser.add_argument("--siglas", help="Lista separada por virgula. Default: todas da planilha.")
    parser.add_argument("--planilha", default=str(Path.home() / "Empresas.xlsx"))
    parser.add_argument("--docs-dir", default=str(REPO_ROOT / "erp" / "downloads"))
    parser.add_argument("--report", help="Default: sharepoint/reports/prolabore_MM_AAAA_report.json")
    parser.add_argument(
        "--execucao-assignee",
        default=str(DEFAULT_EXECUCAO_ASSIGNEE_ID),
        help="ID interno, nome exato ou e-mail do responsavel da etapa Execucao.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        comp = Competencia(args.competencia)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    empresas = aplicar_trimestral(load_empresas(Path(args.planilha)), comp.mes)
    if args.siglas:
        pedidas = [s.strip() for s in args.siglas.split(",") if s.strip()]
        por_sigla = {e.sigla.casefold(): e for e in empresas}
        faltando = [s for s in pedidas if s.casefold() not in por_sigla]
        for sigla in faltando:
            emit({"tipo": "empresa", "sigla": sigla, "nome": sigla, "status": "pulada",
                  "detalhe": "fora da planilha (Processar≠SIM ou trimestral fora do mês)"})
        empresas = [por_sigla[s.casefold()] for s in pedidas if s.casefold() in por_sigla]

    report_path = Path(args.report) if args.report else (
        REPO_ROOT / "sharepoint" / "reports" / f"prolabore_{comp.mes:02d}_{comp.ano}_report.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    emit({"tipo": "inicio", "total": len(empresas), "competencia": comp.portal, "report": str(report_path)})

    settings = SharePointSettings.from_env()
    client = SharePointClient(settings)
    execucao_assignee_id = resolve_user_info_id(client, args.execucao_assignee)
    client_rows = fetch_all_clients(client)
    client_index = build_client_index(client_rows)
    client_by_id = {int(r["ID"]): r for r in client_rows}

    results: list[dict[str, Any]] = []
    for empresa_row in empresas:
        try:
            result = process_empresa(
                client, comp, client_index, client_by_id, empresa_row,
                Path(args.docs_dir), args.dry_run, execucao_assignee_id,
            )
        except Exception as exc:
            result = {"sigla": empresa_row.sigla, "empresa": empresa_row.empresa, "status": "failed", "error": str(exc)}
        results.append(result)
        status, detalhe = resumo(result)
        emit({"tipo": "empresa", "sigla": empresa_row.sigla, "nome": empresa_row.empresa,
              "status": status, "detalhe": detalhe})
        report_path.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    falhas = sum(1 for r in results if r.get("status") == "failed")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())
