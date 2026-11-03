from __future__ import annotations

import json
import os
import re
import threading
import traceback
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, render_template, request

import openpyxl

from serpro_client import SerproClient, SerproSettings
from sharepoint_client import SharePointClient, SharePointSettings
import portal_tarefas
from pdf_utils import extrair_vencimento_darf
from vault_secrets import get_vault_secrets

app = Flask(__name__)

BASE_DIR = Path(__file__).parent
# Lista única compartilhada com a folha/pró-labore ERP (Sigla|Empresa|Processar|CNPJ|Obs).
# Override via env EMPRESAS_XLSX.
PROLABORE_XLSX = Path(os.environ.get("EMPRESAS_XLSX", Path.home() / "Empresas.xlsx"))
SELECOES_DIR = BASE_DIR / "selecoes"
SELECOES_DIR.mkdir(exist_ok=True)

# Estado dos lotes em processamento em segundo plano. Em memoria do
# processo - por isso o gunicorn roda com 1 worker so (ver systemd unit),
# senao um poll de /status podia cair num worker diferente do que rodou o
# /gerar e nao achar o job.
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def load_env(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


CONFIG = load_env(BASE_DIR / ".env")


def _carregar_config_do_vault() -> None:
    """Preenche CONFIG com segredos vindos do Vault (kv secret/dctfweb), mantendo
    os mesmos nomes de chave usados antes no .env, pra nao precisar tocar em
    todos os pontos de uso. Segredos de certificado ficam em CONFIG_CERTS
    porque sao bytes, nao string."""
    vault = get_vault_secrets(bootstrap=CONFIG)
    vault_config = vault.get_config()
    CONFIG["SERPRO_CONSUMER_KEY"] = vault_config["serpro_consumer_key"]
    CONFIG["SERPRO_CONSUMER_SECRET"] = vault_config["serpro_consumer_secret"]
    CONFIG["SERPRO_CNPJ_ESCRITORIO"] = vault_config["serpro_cnpj_escritorio"]
    CONFIG["SHAREPOINT_TENANT_ID"] = vault_config["sharepoint_tenant_id"]
    CONFIG["SHAREPOINT_CLIENT_ID"] = vault_config["sharepoint_client_id"]
    CONFIG["SHAREPOINT_SITE_URL"] = vault_config["sharepoint_site_url"]
    CONFIG["SHAREPOINT_DCTFWEB_FOLDER"] = vault_config["sharepoint_dctfweb_folder"]


_carregar_config_do_vault()


def normalize_cnpj(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def load_prolabore() -> list[dict]:
    if not PROLABORE_XLSX.exists():
        return []
    wb = openpyxl.load_workbook(PROLABORE_XLSX, read_only=True)
    ws = wb["Empresas"] if "Empresas" in wb.sheetnames else wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))

    header_idx = None
    for i, row in enumerate(rows):
        if row and row[0] == "Sigla":
            header_idx = i
            break
    if header_idx is None:
        return []

    empresas = []
    for row in rows[header_idx + 1 :]:
        if not row or not row[1]:
            continue
        sigla, nome, processar, cnpj, obs = (tuple(row) + (None,) * 5)[:5]
        if not nome or not cnpj:
            continue
        empresas.append(
            {
                "sigla": (sigla or "").strip(),
                "nome": str(nome).strip(),
                "cnpj": normalize_cnpj(str(cnpj)),
                "cnpj_fmt": str(cnpj).strip(),
                "processar_planilha": str(processar or "").strip().upper(),
                "obs": str(obs or "").strip(),
            }
        )
    return empresas


TRIMESTRAL_MESES = {3, 6, 9, 12}


def empresas_do_lote(competencia: str) -> list[dict]:
    """Empresas da planilha que entram no lote automático de uma competência MM/AAAA:
    Processar=SIM e, se marcadas TRIMESTRAL na coluna Obs, só nos meses 03/06/09/12."""
    mes = int(parse_competencia(competencia)[1])
    selecionados = []
    for e in load_prolabore():
        if e["processar_planilha"] != "SIM":
            continue
        if "TRIMESTRAL" in e["obs"].upper() and mes not in TRIMESTRAL_MESES:
            continue
        selecionados.append({"cnpj": e["cnpj"], "nome": e["nome"], "aba": "prolabore"})
    return selecionados


def load_geral() -> list[dict]:
    """
    Aba Geral esvaziada por decisao operacional (2026-08-12): ainda nao
    existe a planilha definitiva de "empresas geral" e a geracao real ja
    esta conectada (SERPRO + SharePoint), entao evitamos rodar sobre a
    lista placeholder do Hub para nao gerar documentos por engano.
    Repovoar quando a planilha definitiva for definida.
    """
    return []


def competencia_padrao() -> str:
    hoje = datetime.now()
    primeiro_dia_mes_atual = hoje.replace(day=1)
    mes_anterior = primeiro_dia_mes_atual - timedelta(days=1)
    return mes_anterior.strftime("%m/%Y")


def parse_competencia(competencia: str) -> tuple[str, str]:
    """Valida MM/AAAA. O mes precisa existir (01-12): sem essa checagem, um "13/2026"
    passava batido e o lote saia rodando as 67 empresas contra um mes inexistente
    (aconteceu em 2026-09-15 - a SERPRO recusou tudo, mas a pasta da competencia
    chegou a ser criada no SharePoint)."""
    m = re.match(r"^(\d{2})/(\d{4})$", competencia.strip())
    if not m:
        raise ValueError(f"Competência inválida: '{competencia}'. Use o formato MM/AAAA.")
    mes, ano = m.group(1), m.group(2)
    if not 1 <= int(mes) <= 12:
        raise ValueError(f"Competência inválida: '{competencia}'. O mês precisa estar entre 01 e 12.")
    return ano, mes


def get_serpro_client() -> SerproClient:
    cert = get_vault_secrets().get_certificado("_internal_serpro-integra-contador")
    return SerproClient(
        SerproSettings(
            consumer_key=CONFIG["SERPRO_CONSUMER_KEY"],
            consumer_secret=CONFIG["SERPRO_CONSUMER_SECRET"],
            cert_bytes=cert.pfx_bytes,
            cert_password=cert.password,
            cnpj_escritorio=CONFIG["SERPRO_CNPJ_ESCRITORIO"],
        )
    )


def get_sharepoint_client() -> SharePointClient:
    cert = get_vault_secrets().get_certificado("_internal_sharepoint-dctfweb")
    return SharePointClient(
        SharePointSettings(
            tenant_id=CONFIG["SHAREPOINT_TENANT_ID"],
            client_id=CONFIG["SHAREPOINT_CLIENT_ID"],
            cert_bytes=cert.pfx_bytes,
            cert_password=cert.password,
            site_url=CONFIG["SHAREPOINT_SITE_URL"],
        )
    )


# Empresas em que as 2 primeiras palavras não distinguem: sem a exceção, "EMPRESA X DO BRASIL" e
# "EMPRESA X SÃO PAULO" virariam o mesmo nome de arquivo e uma sobrescreveria a outra na pasta.
# A chave é o começo do nome, em maiúsculo, e pode ter mais de 2 palavras.
NOME_ARQUIVO_ESPECIAL = {
    "EMPRESA X DO": "EMPRESA X BRASIL",
    "EMPRESA X SÃO": "EMPRESA X SÃO PAULO",
    "CLINICA ODONT. ALFA": "CLINICA ALFA",
    "CLINICA ODONT. BETA": "CLINICA BETA",
}


def nome_arquivo(nome_empresa: str) -> str:
    """Usa só as 2 primeiras palavras do nome da empresa, em maiúsculo, pro nome do arquivo,
    salvo as exceções de NOME_ARQUIVO_ESPECIAL (casadas pelo começo do nome, palavra inteira).

    Descarta tokens sem letra/número (hífen, "&" soltos): sem isso, um nome como
    "HDA - Industria..." virava "HDA -" e o arquivo saía "HDA - - DARF DCTFWEB - MM.AAAA.pdf".
    """
    partes = [t for t in nome_empresa.strip().split() if any(ch.isalnum() for ch in t)]
    completo = " ".join(partes).upper()
    for chave in sorted(NOME_ARQUIVO_ESPECIAL, key=len, reverse=True):
        if completo == chave or completo.startswith(chave + " "):
            return NOME_ARQUIVO_ESPECIAL[chave]
    return " ".join(partes[:2]).upper()


def gerar_documentos_empresa(
    serpro: SerproClient, sp: SharePointClient, folder_competencia: str,
    nome_empresa: str, cnpj: str, ano_pa: str, mes_pa: str, competencia_fmt: str,
    incluir_declaracao_completa: bool = True,
) -> dict:
    nome_upper = nome_arquivo(nome_empresa)
    resultado = {"empresa": nome_empresa, "cnpj": cnpj, "arquivos": [], "arquivos_bytes": [], "erro": None}
    try:
        darf = serpro.gerar_darf(cnpj, ano_pa, mes_pa)
        darf_nome = f"{nome_upper} - DARF DCTFWEB - {competencia_fmt}.pdf"
        sp.upload_file(folder_competencia, darf_nome, darf["pdf_bytes"])
        resultado["arquivos"].append("DARF")
        vencimento_darf = extrair_vencimento_darf(darf["pdf_bytes"])
        resultado["arquivos_bytes"].append((darf_nome, darf["pdf_bytes"], vencimento_darf))

        recibo = serpro.consultar_recibo(cnpj, ano_pa, mes_pa)
        recibo_nome = f"{nome_upper} - RECIBO DCTFWEB - {competencia_fmt}.pdf"
        sp.upload_file(folder_competencia, recibo_nome, recibo["pdf_bytes"])
        resultado["arquivos"].append("RECIBO")
        resultado["arquivos_bytes"].append((recibo_nome, recibo["pdf_bytes"], None))

        if incluir_declaracao_completa:
            declaracao = serpro.consultar_declaracao_completa(cnpj, ano_pa, mes_pa)
            declaracao_nome = f"{nome_upper} - DECLARACAO COMPLETA DCTFWEB - {competencia_fmt}.pdf"
            sp.upload_file(folder_competencia, declaracao_nome, declaracao["pdf_bytes"])
            resultado["arquivos"].append("DECLARACAO COMPLETA")
            resultado["arquivos_bytes"].append((declaracao_nome, declaracao["pdf_bytes"], None))
    except Exception as exc:
        resultado["erro"] = str(exc)
        app.logger.error("Erro gerando documentos de %s (%s): %s", nome_empresa, cnpj, traceback.format_exc())
    return resultado


def processar_portal_empresa(
    sp: SharePointClient, clientes_index: dict, cnpj: str, aba: str,
    ano_pa: str, mes_pa: str, arquivos_bytes: list[tuple[str, bytes, str | None]], finalizar: bool,
) -> dict:
    try:
        return portal_tarefas.processar_tarefa_portal(
            sp, clientes_index, cnpj, aba, ano_pa, mes_pa, arquivos_bytes, finalizar
        )
    except Exception as exc:
        app.logger.error("Erro no portal de tarefas para CNPJ %s: %s", cnpj, traceback.format_exc())
        return {"erro": str(exc)}


def classificar_resultado(resultado: dict, anexar_portal: bool, finalizar_tarefa: bool) -> dict:
    """
    Decide se a empresa ficou "completa" pro que foi pedido:
    - documento gerado sem erro
    - se "anexar no portal" foi marcado: anexado sem erro
    - se "finalizar tarefa" foi marcado: tarefa realmente finalizada
    Devolve {"completa": bool, "motivo": str|None} - motivo só quando incompleta.
    """
    if resultado.get("pulada"):
        return {"completa": False, "motivo": resultado["pulada"]}
    if resultado["erro"]:
        return {"completa": False, "motivo": f"Falha ao gerar documentos: {resultado['erro']}"}
    if not anexar_portal:
        return {"completa": True, "motivo": None}
    portal = resultado.get("portal")
    if not portal:
        return {"completa": False, "motivo": "Anexo no portal não foi processado"}
    if portal.get("erro"):
        return {"completa": False, "motivo": f"Falha ao anexar no portal: {portal['erro']}"}
    if portal.get("aviso"):
        return {"completa": False, "motivo": portal["aviso"]}
    if finalizar_tarefa and not portal.get("finalizada"):
        return {"completa": False, "motivo": "Anexado no portal, mas não foi finalizado"}
    return {"completa": True, "motivo": None}


def processar_lote_async(
    job_id: str, selecionados: list[dict], ano_pa: str, mes_pa: str, competencia: str,
    competencia_fmt: str, folder_competencia: str, folder_competencia_nome: str,
    anexar_portal: bool, finalizar_tarefa: bool, out_file: Path, log_payload: dict,
) -> None:
    try:
        serpro = get_serpro_client()
        sp = get_sharepoint_client()
        sp.ensure_folder(folder_competencia)

        # Varredura antes de gerar: o cadastro do portal ("Cliente Ativo (S/N)") manda.
        # Empresa inativa nao gera nada - nem gasta chamada na SERPRO.
        clientes_index = portal_tarefas.fetch_all_clientes_index(sp)
        inativos = {
            cnpj for cnpj, row in clientes_index.items() if portal_tarefas.cliente_inativo(row)
        }

        for empresa in selecionados:
            if empresa["cnpj"] in inativos:
                resultado = {
                    "empresa": empresa["nome"], "cnpj": empresa["cnpj"], "arquivos": [],
                    "portal": None, "erro": None,
                    "pulada": "Cliente marcado como inativo no cadastro do portal — nada foi gerado",
                }
                resultado["classificacao"] = classificar_resultado(resultado, anexar_portal, finalizar_tarefa)
                with JOBS_LOCK:
                    JOBS[job_id]["resultados"].append(resultado)
                    JOBS[job_id]["processados"] = len(JOBS[job_id]["resultados"])
                continue

            resultado = gerar_documentos_empresa(
                serpro, sp, folder_competencia,
                empresa["nome"], empresa["cnpj"], ano_pa, mes_pa, competencia_fmt,
                incluir_declaracao_completa=(empresa["aba"] != "prolabore"),
            )
            if anexar_portal and not resultado["erro"]:
                resultado["portal"] = processar_portal_empresa(
                    sp, clientes_index, empresa["cnpj"], empresa["aba"],
                    ano_pa, mes_pa, resultado["arquivos_bytes"], finalizar_tarefa,
                )
            else:
                resultado["portal"] = None
            del resultado["arquivos_bytes"]
            resultado["classificacao"] = classificar_resultado(resultado, anexar_portal, finalizar_tarefa)

            with JOBS_LOCK:
                JOBS[job_id]["resultados"].append(resultado)
                JOBS[job_id]["processados"] = len(JOBS[job_id]["resultados"])

        with JOBS_LOCK:
            JOBS[job_id]["status"] = "concluido"
    except Exception as exc:
        app.logger.error("Erro fatal no lote %s: %s", job_id, traceback.format_exc())
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "erro_fatal"
            JOBS[job_id]["erro_fatal"] = str(exc)

    with JOBS_LOCK:
        job_snapshot = dict(JOBS[job_id])
    log_payload["resultados"] = job_snapshot["resultados"]
    log_payload["erro_fatal"] = job_snapshot.get("erro_fatal")
    out_file.write_text(json.dumps(log_payload, indent=2, ensure_ascii=False))



def anexar_status(empresas: list[dict], status_por_cnpj: dict[str, str]) -> list[dict]:
    return [{**e, "status": status_por_cnpj.get(e["cnpj"], "")} for e in empresas]


@app.route("/", methods=["GET"])
def index():
    geral = load_geral()
    prolabore = load_prolabore()
    competencia = request.args.get("competencia", "").strip() or competencia_padrao()

    status_msg = None
    try:
        ano_pa, mes_pa = parse_competencia(competencia)
        folder_competencia_nome = f"{mes_pa} {ano_pa}"
        folder_competencia = f"{CONFIG['SHAREPOINT_DCTFWEB_FOLDER']}/{folder_competencia_nome}"
        sp = get_sharepoint_client()
        clientes_index = portal_tarefas.fetch_all_clientes_index(sp)

        status_geral = portal_tarefas.montar_status_empresas(
            sp, geral, "geral", ano_pa, mes_pa, clientes_index, folder_competencia, nome_arquivo
        )
        status_prolabore = portal_tarefas.montar_status_empresas(
            sp, prolabore, "prolabore", ano_pa, mes_pa, clientes_index, folder_competencia, nome_arquivo
        )
        geral = anexar_status(geral, status_geral)
        prolabore = anexar_status(prolabore, status_prolabore)
    except Exception as exc:
        app.logger.error("Erro calculando status: %s", traceback.format_exc())
        status_msg = f"Não foi possível calcular o status das empresas: {exc}"
        geral = anexar_status(geral, {})
        prolabore = anexar_status(prolabore, {})

    return render_template(
        "index.html",
        geral=geral,
        prolabore=prolabore,
        competencia_padrao=competencia,
        flash_msg=status_msg,
        flash_class="warn",
        resultados=None,
    )


@app.route("/gerar", methods=["POST"])
def gerar():
    geral_sel = request.form.getlist("geral")
    prolabore_sel = request.form.getlist("prolabore")
    competencia = request.form.get("competencia", "").strip()
    anexar_portal = request.form.get("anexar_portal") == "1"
    finalizar_tarefa = anexar_portal and request.form.get("finalizar_tarefa") == "1"

    geral = load_geral()
    prolabore = load_prolabore()
    nome_por_cnpj = {e["cnpj"]: e["nome"] for e in geral + prolabore}

    selecionados = []
    pagina_desatualizada = False
    for raw, aba in [(r, "geral") for r in geral_sel] + [(r, "prolabore") for r in prolabore_sel]:
        cnpj, sep, nome = raw.partition("|")
        if not sep or not nome:
            nome = nome_por_cnpj.get(cnpj, "")
            pagina_desatualizada = True
        selecionados.append({"cnpj": cnpj, "nome": nome, "aba": aba})

    if pagina_desatualizada:
        return render_template(
            "index.html", geral=geral, prolabore=prolabore, competencia_padrao=competencia,
            flash_msg=(
                "A página estava desatualizada (aberta antes de uma atualização do site) — "
                "nada foi gerado. <strong>Recarregue a página (F5) e tente de novo.</strong>"
            ),
            flash_class="warn", resultados=None,
        )

    if not selecionados:
        return render_template(
            "index.html", geral=geral, prolabore=prolabore, competencia_padrao=competencia,
            flash_msg="Nenhuma empresa selecionada.", flash_class="warn", resultados=None,
        )

    try:
        ano_pa, mes_pa = parse_competencia(competencia)
    except ValueError as exc:
        return render_template(
            "index.html", geral=geral, prolabore=prolabore, competencia_padrao=competencia_padrao(),
            flash_msg=str(exc), flash_class="warn", resultados=None,
        )

    job_id = _iniciar_job(selecionados, competencia, ano_pa, mes_pa, anexar_portal, finalizar_tarefa)
    return render_template("progress.html", job_id=job_id, total=len(selecionados), competencia=competencia)


def _iniciar_job(
    selecionados: list[dict], competencia: str, ano_pa: str, mes_pa: str,
    anexar_portal: bool, finalizar_tarefa: bool,
) -> str:
    """Cria o job em memória e dispara o lote em background. Devolve o job_id.
    Usado tanto pela UI (/gerar) quanto pelo disparo automático (/gerar-lote)."""
    competencia_fmt = f"{mes_pa}.{ano_pa}"
    folder_competencia_nome = f"{mes_pa} {ano_pa}"
    folder_competencia = f"{CONFIG['SHAREPOINT_DCTFWEB_FOLDER']}/{folder_competencia_nome}"

    log_payload = {
        "timestamp": datetime.now().isoformat(),
        "competencia": competencia,
        "anexar_portal": anexar_portal,
        "finalizar_tarefa": finalizar_tarefa,
        "selecionados": selecionados,
    }
    out_file = SELECOES_DIR / f"selecao-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"

    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "processando",
            "total": len(selecionados),
            "processados": 0,
            "resultados": [],
            "competencia": competencia,
            "folder_competencia_nome": folder_competencia_nome,
            "anexar_portal": anexar_portal,
            "finalizar_tarefa": finalizar_tarefa,
            "log_file": out_file.name,
        }

    thread = threading.Thread(
        target=processar_lote_async,
        args=(
            job_id, selecionados, ano_pa, mes_pa, competencia, competencia_fmt,
            folder_competencia, folder_competencia_nome, anexar_portal, finalizar_tarefa,
            out_file, log_payload,
        ),
        daemon=True,
    )
    thread.start()
    return job_id


@app.route("/gerar-lote", methods=["POST"])
def gerar_lote():
    """Disparo automático (n8n, dia 13): roda a planilha inteira sem a UI de seleção.

    Corpo JSON (todos opcionais):
      competencia       "MM/AAAA" — default: mês anterior
      anexar_portal     bool — default true
      finalizar_tarefa  bool — default true (ignorado se anexar_portal=false)
      cnpjs             [str] — restringe o lote a esses CNPJs (painel de automações).
                        Ausente = planilha inteira. Presente e vazio = erro, nunca "todas".

    Resposta: {job_id, total, competencia, empresas:[...siglas...]}.
    Acompanhar em GET /status/<job_id>.
    """
    body = request.get_json(silent=True) or {}
    competencia = str(body.get("competencia") or "").strip() or competencia_padrao()
    anexar_portal = bool(body.get("anexar_portal", True))
    finalizar_tarefa = anexar_portal and bool(body.get("finalizar_tarefa", True))

    try:
        ano_pa, mes_pa = parse_competencia(competencia)
    except ValueError as exc:
        return jsonify({"erro": str(exc)}), 400

    selecionados = empresas_do_lote(competencia)
    if "cnpjs" in body:
        pedidos = body.get("cnpjs")
        if not isinstance(pedidos, list) or not pedidos:
            return jsonify({"erro": "cnpjs precisa ser uma lista não vazia.", "competencia": competencia}), 400
        pedidos = {re.sub(r"\D", "", str(c)) for c in pedidos}
        selecionados = [s for s in selecionados if re.sub(r"\D", "", s["cnpj"]) in pedidos]
        if not selecionados:
            return jsonify({"erro": "Nenhum dos CNPJs pedidos entra no lote desta competência.", "competencia": competencia}), 400
    if not selecionados:
        return jsonify({"erro": "Nenhuma empresa com Processar=SIM na planilha.", "competencia": competencia}), 400

    job_id = _iniciar_job(selecionados, competencia, ano_pa, mes_pa, anexar_portal, finalizar_tarefa)
    return jsonify({
        "job_id": job_id,
        "total": len(selecionados),
        "competencia": competencia,
        "anexar_portal": anexar_portal,
        "finalizar_tarefa": finalizar_tarefa,
        "empresas": [s["nome"] for s in selecionados],
    })


@app.route("/status/<job_id>", methods=["GET"])
def status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return jsonify({"status": "nao_encontrado"}), 404
        snapshot = dict(job)
    return jsonify(snapshot)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8789)
