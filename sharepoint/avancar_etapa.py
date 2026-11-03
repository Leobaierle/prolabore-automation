#!/usr/bin/env python3
"""Avança UMA tarefa do Portal de Revisão e Validação para Publicação.

Reproduz o que o Portal grava quando o revisor avança a etapa (conferido no histórico de
versões da tarefa 83701, v5.0): PassoAtual=Publicacao, PassoAnterior=RevisaoEValidacao,
UsuarioAtualId=None, RevisaoEValidacaoFim/PublicacaoInicio=agora,
RevisaoEValidacaoUserAprovId=<revisor>.

Só aceita tarefa que ESTEJA em RevisaoEValidacao (qualquer outro estado aborta sem alterar).

Uso (de ~/Prolabore):
  python3 sharepoint/avancar_etapa.py 83701 [--dry-run] [--revisor 31]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _load_env_sharepoint() -> None:
    candidatos = [REPO_ROOT / ".env.sharepoint", Path.home() / "work" / "Sharepoint-API" / ".env.sharepoint"]
    arquivo = next((p for p in candidatos if p.is_file()), None)
    if arquivo is None:
        raise SystemExit(".env.sharepoint nao encontrado")
    for line in arquivo.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


_load_env_sharepoint()

from sharepoint_client import SharePointClient, SharePointSettings, update_task_item  # noqa: E402

COLS = ("ID,PassoAtual,PassoAnterior,UsuarioAtualId,ExecucaoUserAprovId,RevisaoEValidacaoFim,"
        "RevisaoEValidacaoUserAprovId,PublicacaoInicio,PublicacaoFim")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task_id", type=int)
    ap.add_argument("--revisor", type=int, default=31, help="id do usuário que aprova a revisão (ex.: 31)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    client = SharePointClient(SharePointSettings.from_env())
    site = client.settings.task_site_url
    path = f"/_api/web/lists/getbytitle(%27Tarefas%27)/items({a.task_id})?$select={COLS}"

    def ler() -> dict:
        d = client.rest_json(site, path)["d"]
        return {k: v for k, v in d.items() if not k.startswith("__")}

    antes = ler()
    print("ANTES ", json.dumps(antes, ensure_ascii=False))
    if antes.get("PassoAtual") != "RevisaoEValidacao":
        print(f"ABORTADO: a tarefa está em {antes.get('PassoAtual')!r}, não em RevisaoEValidacao. Nada foi alterado.")
        return 1

    agora = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "PassoAtual": "Publicacao",
        "PassoAnterior": "RevisaoEValidacao",
        "UsuarioAtualId": None,
        "RevisaoEValidacaoFim": agora,
        "RevisaoEValidacaoUserAprovId": a.revisor,
        "PublicacaoInicio": agora,
    }
    print("ALTERA", json.dumps(payload, ensure_ascii=False))
    if a.dry_run:
        print("dry-run: nada foi alterado.")
        return 0

    update_task_item(client, a.task_id, "SP.Data.TarefasListItem", payload)
    depois = ler()
    print("DEPOIS", json.dumps(depois, ensure_ascii=False))
    return 0 if depois.get("PassoAtual") == "Publicacao" else 2


if __name__ == "__main__":
    raise SystemExit(main())
