from __future__ import annotations

import os
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any

from sharepoint_client import SharePointClient

TASK_SITE_URL = "https://empresa.sharepoint.com/sites/Empresa/portaldetarefas"
TASK_LIST_GUID = os.environ.get("TASK_LIST_GUID", "00000000-0000-0000-0000-000000000000")
AREA_ID = 3

# ProcessoId no portal de tarefas.
PROCESSO_DCTF_PROLABORE = 103  # rotina "dctfweb_mensal" - empresas da aba Prolabore
PROCESSO_FGTS_DCTFWEB_GERAL = 110  # rotina "fgts_dctfweb_mensal" - empresas da aba Geral

MONTH_NAMES_PT = {
    1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril", 5: "Maio", 6: "Junho",
    7: "Julho", 8: "Agosto", 9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro",
}

# Responsavel da Execucao: cada cliente tem o seu, no campo ResponsavelFolha
# ("Colaborador Pessoal") da lista Clientes do portal - e de la que sai o
# UsuarioAtualId da tarefa criada. Este id so e usado como fallback, para os
# clientes que ainda nao tem o campo preenchido (decidido com o usuario em
# (responsável padrão).
FALLBACK_EXECUCAO_ASSIGNEE_ID = 343703

# Responsavel da etapa Revisao e Validacao: sempre o mesmo revisor, em qualquer carteira
# (mesma regra do robô do pró-labore). Quem executou fica registrado em ExecucaoUserAprovId.
REVISAO_ASSIGNEE_ID = 31


# Campo "Cliente Ativo (S/N)" da lista Clientes do portal - e o cadastro que diz se a
# empresa ainda e cliente. O robo passou a conferir em 2026-09-15, depois de gerar e
# anexar documentos para um cliente no mesmo dia em que ela foi marcada como inativa.
CAMPO_CLIENTE_ATIVO = "Cliente_x0020_Ativo_x0020__x0028"


def cliente_inativo(cliente_row: dict[str, Any] | None) -> bool:
    """True so quando o cadastro diz explicitamente que nao e mais cliente.
    Campo ausente (None) nao conta como inativo - evita bloquear tudo se o
    nome interno do campo mudar."""
    if not cliente_row:
        return False
    return cliente_row.get(CAMPO_CLIENTE_ATIVO) is False


def normalize_cnpj(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def fetch_tarefas_por_processo(client: SharePointClient, processo_id: int, competencia: str) -> dict[int, dict]:
    """
    Busca todas as tarefas de um processo+competencia de uma vez (nao uma
    por empresa) e indexa por ClienteId. Usado pra montar a coluna Status
    sem precisar de N chamadas.
    """
    params = {
        "$filter": f"ProcessoId eq {processo_id} and Competencia eq '{competencia}'",
        "$select": "ID,ClienteId,PassoAtual",
        "$top": "5000",
    }
    query = urllib.parse.urlencode(params, safe="%(),'/$")
    rows = client.rest_json(TASK_SITE_URL, f"/_api/web/lists/getbytitle('Tarefas')/items?{query}")["d"]["results"]
    index: dict[int, dict] = {}
    for row in rows:
        cid = int(row["ClienteId"])
        # se tiver mais de uma tarefa pro mesmo cliente+competencia (nao deveria,
        # mas por seguranca), fica com a de ID mais alto (mais recente)
        if cid not in index or int(row["ID"]) > index[cid]["ID"]:
            index[cid] = {"ID": int(row["ID"]), "PassoAtual": row.get("PassoAtual")}
    return index


def fetch_arquivos_dctfweb(client: SharePointClient, dctfweb_folder_competencia: str) -> set[str]:
    """Lista os nomes de arquivo ja salvos na pasta da competencia em Automacao/DCTFWEB."""
    enc = urllib.parse.quote(dctfweb_folder_competencia, safe="/")
    try:
        rows = client.rest_json(
            client.settings.site_url, f"/_api/web/GetFolderByServerRelativeUrl('{enc}')/Files?$select=Name"
        )["d"]["results"]
    except RuntimeError as exc:
        if "HTTP 404" in str(exc):
            return set()
        raise
    return {row["Name"] for row in rows}


def processo_id_por_aba(aba: str) -> int:
    return PROCESSO_DCTF_PROLABORE if aba == "prolabore" else PROCESSO_FGTS_DCTFWEB_GERAL


def competencia_pt(ano: str, mes: str) -> str:
    return f"{MONTH_NAMES_PT[int(mes)]} {ano}"


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def datas_tarefa(ano: str, mes: str) -> dict[str, str]:
    """
    Regra observada em automacao-dctfweb-mensal.md: prazo/vencimento no dia 20
    do mes seguinte a competencia (ex: competencia 07/2026 -> prazo 20/08/2026).
    """
    ano_i, mes_i = int(ano), int(mes)
    mes_seguinte = mes_i + 1
    ano_seguinte = ano_i
    if mes_seguinte > 12:
        mes_seguinte = 1
        ano_seguinte += 1
    prazo = datetime(ano_seguinte, mes_seguinte, 20, tzinfo=timezone.utc)
    execucao_inicio = datetime.now(timezone.utc)
    return {
        "prazo": iso_z(prazo),
        "execucao_inicio": iso_z(execucao_inicio),
        "execucao_prazo": iso_z(prazo),
        "execucao_margem": iso_z(prazo),
    }


def fetch_all_clientes_index(client: SharePointClient) -> dict[str, dict[str, Any]]:
    """Indexa a lista 'Clientes' do portal por CNPJ normalizado (só dígitos)."""
    query = urllib.parse.urlencode(
        {
            "$select": (
                f"ID,Title,CNPJ,SiteCliente,BibliotecaPublicacao,PastaPublicacao,"
                f"ResponsavelFolhaId,ResponsavelFolha/Title,{CAMPO_CLIENTE_ATIVO}"
            ),
            "$expand": "ResponsavelFolha",
            "$top": "5000",
        },
        safe="%(),'/$",
    )
    rows = client.rest_json(TASK_SITE_URL, f"/_api/web/lists/getbytitle('Clientes')/items?{query}")["d"]["results"]
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        cnpj = normalize_cnpj(row.get("CNPJ"))
        if cnpj:
            index[cnpj] = row
    return index


def fetch_existing_tasks(client: SharePointClient, cliente_id: int, processo_id: int, competencia: str) -> list[dict]:
    params = {
        "$filter": f"ClienteId eq {cliente_id} and ProcessoId eq {processo_id} and Competencia eq '{competencia}'",
        "$select": "ID,Title,PassoAtual,UsuarioAtualId",
        "$top": "20",
    }
    query = urllib.parse.urlencode(params, safe="%(),'/$")
    return client.rest_json(TASK_SITE_URL, f"/_api/web/lists/getbytitle('Tarefas')/items?{query}")["d"]["results"]


def criar_tarefa(
    client: SharePointClient, cliente_id: int, processo_id: int, ano: str, mes: str,
    usuario_atual_id: int = FALLBACK_EXECUCAO_ASSIGNEE_ID,
) -> int:
    competencia = competencia_pt(ano, mes)
    datas = datas_tarefa(ano, mes)
    payload = {
        "__metadata": {"type": "SP.Data.TarefasListItem"},
        "Title": "Execução",
        "Action": "Aprovar",
        "AreaId": str(AREA_ID),
        "ProcessoId": str(processo_id),
        "ClienteId": str(cliente_id),
        "Competencia": competencia,
        "Descricao": "",
        "Prazo": datas["prazo"],
        "Origem": "Automatico",
        "PassoAtual": "Execucao",
        "UsuarioAtualId": usuario_atual_id,
        "ExecucaoInicio": datas["execucao_inicio"],
        "ExecucaoPrazo": datas["execucao_prazo"],
        "ExecucaoMargem": datas["execucao_margem"],
        "Vencimento": datas["execucao_prazo"],
        "VencimentoMargem": datas["execucao_margem"],
        "CheckListOption": False,
        "CheckListItems": "[]",
        "NotificacaoCliente": "",
    }
    created = client.rest_json(TASK_SITE_URL, "/_api/web/lists/getbytitle('Tarefas')/items", method="POST", payload=payload)
    return int(created["d"]["ID"])


def fetch_task_full(client: SharePointClient, task_id: int) -> dict[str, Any]:
    # Nao expandir Cliente/SiteCliente aqui - o SharePoint rejeita expandir
    # um campo hyperlink de um lookup aninhado ("A consulta ao campo
    # 'Cliente/SiteCliente' nao e valida", HTTP 400). Os dados do cliente
    # (SiteCliente, BibliotecaPublicacao, PastaPublicacao) ja vem resolvidos
    # separadamente via fetch_all_clientes_index, por CNPJ.
    query = urllib.parse.urlencode(
        {
            "$filter": f"ID eq {task_id}",
            "$select": (
                "*,Cliente/ID,Cliente/Title,Processo/ID,Processo/Title,Area/ID,Area/Title,"
                "UsuarioAtual/ID,UsuarioAtual/Title,UsuarioAtual/EMail"
            ),
            "$expand": "Cliente,Processo,Area,UsuarioAtual",
        }
    )
    result = client.rest_json(TASK_SITE_URL, f"/_api/Web/Lists(guid'{TASK_LIST_GUID}')/Items?{query}")
    rows = result["d"]["results"]
    if not rows:
        raise RuntimeError(f"Tarefa {task_id} não encontrada")
    return rows[0]


def resolve_library(client: SharePointClient, client_site_url: str, expected_title: str) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {
            "$select": "Id,Title,ListItemEntityTypeFullName,BaseTemplate,RootFolder/ServerRelativeUrl,RootFolder/Name",
            "$expand": "RootFolder",
        }
    )
    rows = client.rest_json(client_site_url, f"/_api/web/lists?{query}")["d"]["results"]
    matches = [r for r in rows if r.get("BaseTemplate") == 101 and r.get("Title") == expected_title]
    if not matches:
        raise RuntimeError(f"Biblioteca '{expected_title}' não encontrada em {client_site_url}")
    return matches[0]


# Campo interno da biblioteca do cliente (título exibido: "Data Vencimento
# Anexo") - confirmado em 2026-08-12 consultando os fields da lista
# "Documentos Compartilhados" no site de um cliente real.
CAMPO_VENCIMENTO_ANEXO = "Data_x0020_Vencimento_x0020_Anexo"


def arquivo_ja_anexado(
    client: SharePointClient, client_site_url: str, folder_server_relative_url: str,
    file_name: str, task_id: int,
) -> dict[str, Any] | None:
    """
    Verifica se o arquivo ja esta na biblioteca do cliente E vinculado a esta
    tarefa (ItemParentID). Consulta o arquivo direto pela URL - nao e um filtro
    de lista, entao nao esbarra no List View Threshold das bibliotecas grandes.
    Devolve {url, item_id} se ja estiver anexado, senao None (arquivo ausente,
    ou presente mas de outra tarefa - nesse caso vale reenviar).
    """
    url = f"{folder_server_relative_url.rstrip('/')}/{file_name}"
    enc = urllib.parse.quote(url, safe="/")
    try:
        item = client.rest_json(
            client_site_url,
            f"/_api/web/GetFileByServerRelativeUrl('{enc}')/ListItemAllFields?"
            + urllib.parse.urlencode({"$select": "ID,ItemParentID"}),
        )["d"]
    except RuntimeError as exc:
        if "HTTP 404" in str(exc):
            return None
        raise
    if str(item.get("ItemParentID") or "") == str(task_id):
        return {"url": url, "item_id": item.get("ID")}
    return None


def anexar_documento(
    client: SharePointClient, task: dict[str, Any], cliente_row: dict[str, Any], file_name: str, content: bytes,
    vencimento: str | None = None,
) -> dict[str, Any]:
    site_cliente = cliente_row.get("SiteCliente") or {}
    client_site_url = str(site_cliente.get("Url") or "").rstrip("/")
    if not client_site_url:
        raise RuntimeError(f"Cliente '{cliente_row.get('Title')}' sem SiteCliente configurado no portal")

    library_title = cliente_row.get("BibliotecaPublicacao") or "Documentos Compartilhados"
    library = resolve_library(client, client_site_url, str(library_title))
    folder_server_relative_url = library["RootFolder"]["ServerRelativeUrl"]
    pasta_publicacao = cliente_row.get("PastaPublicacao")
    if pasta_publicacao:
        folder_server_relative_url = f"{folder_server_relative_url.rstrip('/')}/{str(pasta_publicacao).strip('/')}"

    ja = arquivo_ja_anexado(client, client_site_url, folder_server_relative_url, file_name, task["ID"])
    if ja:
        return {"file_server_relative_url": ja["url"], "uploaded_item_id": ja["item_id"], "ja_anexado": True}

    uploaded = client.upload_file(folder_server_relative_url, file_name, content, site_url=client_site_url)
    file_server_relative_url = uploaded["ServerRelativeUrl"]

    item_query = urllib.parse.urlencode({"$select": "ID,Id,__metadata/type"})
    item = client.rest_json(
        client_site_url,
        f"/_api/web/GetFileByServerRelativeUrl('{urllib.parse.quote(file_server_relative_url, safe='/')}')/ListItemAllFields?{item_query}",
    )["d"]

    metadata = {
        "ItemParentID": task["ID"],
        "Processo": (task.get("Processo") or {}).get("Title"),
        "Area": (task.get("Area") or {}).get("Title"),
        "Competencia": task.get("Competencia"),
        CAMPO_VENCIMENTO_ANEXO: vencimento,
    }
    metadata = {k: v for k, v in metadata.items() if v not in (None, "")}
    client.rest_json(
        client_site_url,
        f"/_api/web/GetFileByServerRelativeUrl('{urllib.parse.quote(file_server_relative_url, safe='/')}')/ListItemAllFields",
        method="POST",
        payload={"__metadata": {"type": item["__metadata"]["type"]}, **metadata},
        extra_headers={"X-HTTP-Method": "MERGE", "IF-MATCH": "*"},
    )
    return {"file_server_relative_url": file_server_relative_url, "uploaded_item_id": item["ID"], "ja_anexado": False}


def finalizar_tarefa(client: SharePointClient, task: dict[str, Any]) -> dict[str, Any]:
    now = iso_z(datetime.now(timezone.utc))
    execucao_usuario_atual_id = (task.get("UsuarioAtual") or {}).get("ID") or task.get("UsuarioAtualId")
    payload = {
        "Title": "Revisão e Validação",
        "Action": "Aprovar",
        "Descricao": task.get("Descricao"),
        "Competencia": task.get("Competencia"),
        "CheckListOption": task.get("CheckListOption"),
        "CheckListItems": task.get("CheckListItems"),
        "Comments": task.get("Comments") or "",
        "PassoAnterior": "Execucao",
        "PassoAtual": "RevisaoEValidacao",
        "ExecucaoInicio": task.get("ExecucaoInicio"),
        "ExecucaoFim": now,
        "ExecucaoPrazo": task.get("ExecucaoPrazo"),
        "ExecucaoMargem": task.get("ExecucaoMargem"),
        "RevisaoEValidacaoInicio": now,
        "RevisaoEValidacaoPrazo": task.get("RevisaoEValidacaoPrazo"),
        "RevisaoEValidacaoMargem": task.get("RevisaoEValidacaoMargem"),
        "Prazo": task.get("Prazo"),
        "NotificacaoCliente": task.get("NotificacaoCliente") or "",
        "ExecucaoUserAprovId": execucao_usuario_atual_id,
        "UsuarioAtualId": REVISAO_ASSIGNEE_ID,
        "Vencimento": task.get("RevisaoEValidacaoPrazo") or task.get("Vencimento"),
        "VencimentoMargem": task.get("RevisaoEValidacaoMargem") or task.get("VencimentoMargem"),
    }
    client.rest_json(
        TASK_SITE_URL,
        f"/_api/web/lists/getbytitle('Tarefas')/items({task['ID']})",
        method="POST",
        payload={"__metadata": {"type": task["__metadata"]["type"]}, **payload},
        extra_headers={"X-HTTP-Method": "MERGE", "IF-MATCH": "*"},
    )
    return {"task_id": task["ID"], "from_stage": "Execucao", "to_stage": "RevisaoEValidacao"}


def montar_status_empresas(
    client: SharePointClient,
    empresas: list[dict],
    aba: str,
    ano: str,
    mes: str,
    clientes_index: dict[str, dict],
    dctfweb_folder_competencia: str,
    nome_arquivo_fn,
) -> dict[str, str]:
    """
    Monta {cnpj: status} pra uma lista de empresas de uma aba, cruzando:
    - arquivos ja salvos na pasta da competencia em Automacao/DCTFWEB
    - tarefas existentes no portal (ProcessoId da aba + competencia)
    Status possiveis: "" (nada ainda), "Documentos gerados",
    "Anexado no portal", "Finalizado".
    """
    processo_id = processo_id_por_aba(aba)
    competencia = competencia_pt(ano, mes)
    tarefas_por_cliente = fetch_tarefas_por_processo(client, processo_id, competencia)
    arquivos = fetch_arquivos_dctfweb(client, dctfweb_folder_competencia)

    status: dict[str, str] = {}
    for empresa in empresas:
        cnpj = empresa["cnpj"]
        nome_curto = nome_arquivo_fn(empresa["nome"])
        tem_doc = any(a.startswith(f"{nome_curto} - DARF DCTFWEB - ") for a in arquivos)

        cliente_row = clientes_index.get(cnpj)
        tarefa = tarefas_por_cliente.get(int(cliente_row["ID"])) if cliente_row else None

        if tarefa and tarefa["PassoAtual"] != "Execucao":
            status[cnpj] = "Finalizado"
        elif tem_doc and tarefa:
            status[cnpj] = "Anexado no portal"
        elif tem_doc:
            status[cnpj] = "Documentos gerados"
        elif tarefa:
            status[cnpj] = "Tarefa criada"
        else:
            status[cnpj] = ""
    return status


def processar_tarefa_portal(
    client: SharePointClient,
    clientes_index: dict[str, dict[str, Any]],
    cnpj: str,
    aba: str,
    ano: str,
    mes: str,
    arquivos: list[tuple[str, bytes, str | None]],
    finalizar: bool,
) -> dict[str, Any]:
    """
    Verifica/cria a tarefa do portal para a empresa+competencia, anexa os
    arquivos gerados e, se pedido, finaliza (Execucao -> RevisaoEValidacao).
    """
    cnpj_norm = normalize_cnpj(cnpj)
    cliente_row = clientes_index.get(cnpj_norm)
    if not cliente_row:
        raise RuntimeError(f"Cliente com CNPJ {cnpj} não encontrado na lista Clientes do portal")
    if cliente_inativo(cliente_row):
        return {
            "task_id": None, "criada": False, "anexados": [], "ja_anexados": [],
            "finalizada": False,
            "aviso": "Cliente marcado como inativo no cadastro do portal — nada foi anexado",
        }

    cliente_id = int(cliente_row["ID"])
    processo_id = processo_id_por_aba(aba)
    competencia = competencia_pt(ano, mes)

    existentes = fetch_existing_tasks(client, cliente_id, processo_id, competencia)
    if existentes:
        em_execucao = [t for t in existentes if t.get("PassoAtual") == "Execucao"]
        if em_execucao:
            task_id = int(sorted(em_execucao, key=lambda t: int(t["ID"]))[-1]["ID"])
            criada = False
        else:
            # já existe tarefa pra essa competência, mas não está mais em Execucao
            return {
                "task_id": int(existentes[0]["ID"]),
                "criada": False,
                "anexados": [],
                "finalizada": False,
                "aviso": f"Tarefa já existe mas está no passo '{existentes[0].get('PassoAtual')}', não em Execucao — nada foi anexado",
            }
    else:
        responsavel_id = cliente_row.get("ResponsavelFolhaId") or FALLBACK_EXECUCAO_ASSIGNEE_ID
        task_id = criar_tarefa(client, cliente_id, processo_id, ano, mes, usuario_atual_id=int(responsavel_id))
        criada = True

    task = fetch_task_full(client, task_id)
    anexados = []
    ja_anexados = []
    for file_name, content, vencimento in arquivos:
        r = anexar_documento(client, task, cliente_row, file_name, content, vencimento=vencimento)
        (ja_anexados if r.get("ja_anexado") else anexados).append(file_name)

    finalizada = False
    if finalizar:
        task = fetch_task_full(client, task_id)
        finalizar_tarefa(client, task)
        finalizada = True

    return {
        "task_id": task_id, "criada": criada, "anexados": anexados,
        "ja_anexados": ja_anexados, "finalizada": finalizada, "aviso": None,
    }
