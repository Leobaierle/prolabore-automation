from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass

import requests
from requests_pkcs12 import Pkcs12Adapter

AUTH_URL = "https://autenticacao.sapi.serpro.gov.br/authenticate"
GATEWAY_URL = "https://gateway.apiserpro.serpro.gov.br/integra-contador/v1"

# eSocial + Reinf CP + Reinf RET. Exclui Sero (5, traz PIS/COFINS) e MIT (8).
IDS_SISTEMA_ORIGEM_DARF = [1, 6, 7]


# Situacoes conhecidas da DCTFWeb. Sem isso toda falha virava "sem declaracao
# disponivel (provavelmente ainda nao transmitida)", que e enganoso: em 2026-09-15
# essa mensagem generica escondeu um dia inteiro de MG10 (rascunho nao transmitido)
# e fez confundir "sem debito" com "sem declaracao".
SITUACOES = {
    "GUIA03": (
        "não há débito com saldo a pagar nesta competência — não há DARF a emitir. "
        "Sem DARF não se cria tarefa."
    ),
    "MG10": (
        "a declaração mais recente está 'Em Andamento' (rascunho não transmitido). "
        "Transmitir no ERP antes de gerar."
    ),
    "MG08": "não foi encontrada declaração com os dados informados (competência não transmitida?).",
    "MG02": "encaminhamento não autorizado — conferir a procuração eletrônica do cliente.",
    "ICGERENCIADOR-008": "contribuinte recusado pela SERPRO — conferir o CNPJ/CPF no cadastro.",
}


def descrever_falha(id_servico: str, contribuinte: str, status: int, mensagens: list[dict]) -> str:
    """Mensagem de falha que diz o que aconteceu, e nao so que 'nao veio documento'.
    Mantem os codigos e textos originais da SERPRO no fim, para auditoria."""
    detalhe = "; ".join(f"{m.get('codigo', '')}: {m.get('texto', '')}" for m in mensagens)
    for chave, explicacao in SITUACOES.items():
        if any(chave in str(m.get("codigo", "")) for m in mensagens):
            return f"{id_servico} ({contribuinte}): {explicacao} [{detalhe}]"
    if status != 200:
        return f"{id_servico} ({contribuinte}) falhou com HTTP {status}. [{detalhe or 'sem mensagens'}]"
    return (
        f"{id_servico} ({contribuinte}): a SERPRO não devolveu documento. "
        f"[{detalhe or 'sem mensagens'}]"
    )


@dataclass
class SerproSettings:
    consumer_key: str
    consumer_secret: str
    cert_bytes: bytes
    cert_password: str
    cnpj_escritorio: str


class SerproClient:
    def __init__(self, settings: SerproSettings) -> None:
        self.settings = settings
        self.session = requests.Session()
        self.session.mount(
            "https://",
            Pkcs12Adapter(pkcs12_data=settings.cert_bytes, pkcs12_password=settings.cert_password),
        )
        self._access_token: str | None = None
        self._jwt_token: str | None = None
        self._expiry = 0.0

    def _authenticate(self) -> None:
        now = time.time()
        if self._access_token and now < self._expiry - 60:
            return
        basic = base64.b64encode(
            f"{self.settings.consumer_key}:{self.settings.consumer_secret}".encode()
        ).decode()
        resp = self.session.post(
            AUTH_URL,
            headers={
                "Authorization": f"Basic {basic}",
                "role-type": "TERCEIROS",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials"},
            timeout=30,
        )
        resp.raise_for_status()
        auth = resp.json()
        self._access_token = auth["access_token"]
        self._jwt_token = auth["jwt_token"]
        self._expiry = now + int(auth.get("expires_in", 900))

    @staticmethod
    def _tipo_contribuinte(numero: str) -> int:
        """1 = CPF (11 digitos), 2 = CNPJ (14). A SERPRO recusa com
        'Contribuinte: CNPJ invalido' se mandarmos um CPF como tipo 2."""
        digitos = re.sub(r"\D", "", numero or "")
        if len(digitos) == 11:
            return 1
        if len(digitos) == 14:
            return 2
        raise ValueError(
            f"Documento do contribuinte invalido: '{numero}' tem {len(digitos)} digitos "
            f"(esperado 11 para CPF ou 14 para CNPJ). Corrija o cadastro."
        )

    def _call(self, endpoint: str, cnpj_cliente: str, id_servico: str, dados: dict) -> dict:
        self._authenticate()
        body = {
            "contratante": {"numero": self.settings.cnpj_escritorio, "tipo": 2},
            "autorPedidoDados": {"numero": self.settings.cnpj_escritorio, "tipo": 2},
            "contribuinte": {"numero": cnpj_cliente, "tipo": self._tipo_contribuinte(cnpj_cliente)},
            "pedidoDados": {
                "idSistema": "DCTFWEB",
                "idServico": id_servico,
                "versaoSistema": "1.0",
                "dados": json.dumps(dados),
            },
        }
        resp = self.session.post(
            f"{GATEWAY_URL}/{endpoint}",
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "jwt_token": self._jwt_token,
                "Content-Type": "application/json",
            },
            json=body,
            timeout=60,
        )
        rj = resp.json()
        mensagens = rj.get("mensagens") or []
        erro_msgs = [m for m in mensagens if str(m.get("codigo", "")).lower().startswith("erro")]
        if resp.status_code != 200 or erro_msgs or not rj.get("dados"):
            raise RuntimeError(descrever_falha(id_servico, cnpj_cliente, resp.status_code, mensagens))
        dados_resp = json.loads(rj["dados"])
        if "PDFByteArrayBase64" not in dados_resp:
            raise RuntimeError(f"{id_servico}: resposta sem PDF. mensagens={mensagens}")
        return {
            "pdf_bytes": base64.b64decode(dados_resp["PDFByteArrayBase64"]),
            "mensagens": mensagens,
        }

    def gerar_darf(self, cnpj_cliente: str, ano_pa: str, mes_pa: str, categoria: str = "GERAL_MENSAL") -> dict:
        dados = {
            "categoria": categoria,
            "anoPA": ano_pa,
            "mesPA": mes_pa,
            "idsSistemaOrigem": IDS_SISTEMA_ORIGEM_DARF,
        }
        return self._call("Emitir", cnpj_cliente, "GERARGUIA31", dados)

    def consultar_recibo(self, cnpj_cliente: str, ano_pa: str, mes_pa: str, categoria: str = "GERAL_MENSAL") -> dict:
        dados = {"categoria": categoria, "anoPA": ano_pa, "mesPA": mes_pa}
        return self._call("Consultar", cnpj_cliente, "CONSRECIBO32", dados)

    def consultar_declaracao_completa(
        self, cnpj_cliente: str, ano_pa: str, mes_pa: str, categoria: str = "GERAL_MENSAL"
    ) -> dict:
        dados = {"categoria": categoria, "anoPA": ano_pa, "mesPA": mes_pa}
        return self._call("Consultar", cnpj_cliente, "CONSDECCOMPLETA33", dados)
