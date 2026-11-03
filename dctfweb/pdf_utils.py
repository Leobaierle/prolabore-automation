from __future__ import annotations

import re
import subprocess
import tempfile
from collections import Counter


def extrair_vencimento_darf(pdf_bytes: bytes) -> str | None:
    """
    Le o texto do PDF da DARF DCTFWeb e extrai a data de vencimento
    ("Pagar este documento ate DD/MM/AAAA", tambem repetida no campo
    "Data de Vencimento" no cabecalho). Retorna no formato ISO pronto pro
    campo DateTime do SharePoint, ou None se nao conseguir identificar.

    Usa T03:00:00Z (meia-noite em Brasilia, UTC-3) em vez de T00:00:00Z -
    mesmo padrao ja usado por outras automacoes da empresa nesse campo (ex:
    PGDAS). Gravar T00:00:00Z faz o SharePoint exibir o dia anterior, pois
    converte pro fuso do site (America/Sao_Paulo) antes de mostrar.
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(pdf_bytes)
        tmp.flush()
        try:
            result = subprocess.run(
                ["pdftotext", tmp.name, "-"],
                capture_output=True, timeout=15, check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            return None
    texto = result.stdout.decode("utf-8", errors="replace")

    datas = re.findall(r"\b(\d{2})/(\d{2})/(\d{4})\b", texto)
    if not datas:
        return None

    # a mesma data de vencimento aparece repetida (cabecalho + canhoto de
    # pagamento) - a mais frequente e a data correta.
    mais_comum, _ = Counter(datas).most_common(1)[0]
    dia, mes, ano = mais_comum
    return f"{ano}-{mes}-{dia}T03:00:00Z"
