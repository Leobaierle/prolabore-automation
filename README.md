# Automação do pró-labore (folha → eSocial → SharePoint → DCTFWeb)

Rotina mensal de pró-labore de dezenas de empresas, ponta a ponta: cálculo no sistema de folha, geração dos PDFs
(folha e recibo), envio do eSocial, abertura e anexação das tarefas no portal de tarefas (SharePoint) e, por fim, as
guias da DCTFWeb pela API do SERPRO.

> Projeto real, em produção, publicado aqui sem dados de clientes. Siglas, nomes de empresa, IDs de usuário e
> endereços foram trocados por exemplos.

## Por que é interessante

O sistema de folha não tem API: é uma **aplicação Java servida no navegador (Webswing)**, desenhada num `<canvas>`.
Não há DOM, seletor nem texto para ler. Toda a automação (`folha-rpa/`) é feita com Playwright usando:

- **coordenadas + teclado**, com foco garantido e "espaço-guarda" (o Webswing costuma perder o 1º caractere);
- **leitura de pixel** para saber se um diálogo está aberto (cor da faixa, ícone) e esperar processamento;
- **OCR** (`ocr_linhas.py`) para ler a grade de seleção de empresas e marcar por **CNPJ**, porque a caixa de busca do
  sistema não é confiável e falha com siglas de 2 letras;
- **conferência por contador**: depois de marcar, lê o "Marcados N de M" da janela e só segue se bater com o esperado;
- **login em 2 etapas com limite de 1 sessão** por usuário: logout sempre **verificado** (tela de login ou app
  encerrado) e `teardown` que avisa se teve de fechar sem confirmar.

## As 5 etapas

| # | Etapa | Onde |
|---|---|---|
| 1 | Cálculo do recibo mensal em lote (várias empresas numa janela só) | `folha-rpa/scripts/12-calculo-lote.mjs` |
| 2–3 | Folha (FOPAG) e recibo (RECPAG) em PDF, uma sessão por empresa, com retomada de onde parou | `11-empresa-folha-recibo.mjs`, `20-etapa-calculo-documentos.mjs`, `30-documentos-lote.py` |
| 3 | Envio do eSocial pelo gerenciador do sistema: S-1200 e, só se ele terminar sem erro, S-1210 (nunca o fechamento S-1299, que é outra etapa) | `14-esocial-envio.mjs`, `21-etapa-esocial.mjs` |
| 4–5 | Tarefa no portal de tarefas (lista do SharePoint): cria ou retoma, anexa os PDFs certos e avança para "Revisão e Validação" | `sharepoint/process_prolabore_mes.py`, `sharepoint/avancar_etapa.py` |
| + | DCTFWeb: app Flask que consulta o **SERPRO Integra Contador**, baixa DARF e recibo, sobe na pasta do SharePoint e anexa na tarefa | `dctfweb/` |

Cada etapa escreve uma linha `@@ {json}` por empresa no stdout. Um painel web lê essas linhas e mostra o progresso em
tempo real.

## Destaques do código

- **`folha-rpa/scripts/lib.mjs`**: login (formulário HTML do Webswing + formulário no canvas), troca de empresa pelo
  botão ⇄, logout verificado, `pixel()` e `snap()` para depuração por screenshot.
- **`sharepoint/sharepoint_client.py`**: cliente REST do SharePoint com autenticação **app-only por certificado**
  (Entra ID), retry com backoff e upload de anexos.
- **`sharepoint/process_prolabore_batch.py`**: casa o nome do PDF com o cadastro de clientes (normalização de nome,
  exceções manuais).
- **`dctfweb/portal_tarefas.py`**: confere no cadastro se a empresa ainda é cliente ativo antes de gerar e anexar.
- **`dctfweb/serpro_client.py`**: cliente do Integra Contador (OAuth2 + mTLS com certificado e-CNPJ) e filtro de
  tributos na DARF.
- **`dctfweb/vault_secrets.py`**: segredos no **HashiCorp Vault** (AppRole, kv-v2) e o `.pfx` no **MinIO**, com cache
  e renovação do token.

## Stack

Node 22 + Playwright + pngjs · Python 3.12 (Flask, requests, requests-pkcs12, openpyxl, hvac, minio) · Tesseract (OCR) ·
SharePoint REST / Microsoft Entra ID · SERPRO Integra Contador · HashiCorp Vault · MinIO.

## Rodando

```bash
cd folha-rpa && npm ci && npx playwright install chromium
cp .env.example .env            # URL e credenciais do sistema de folha
SIGLAS=ABC COMPET_MM=09 COMPET_AAAA=2026 STOP=calc-window node scripts/12-calculo-lote.mjs
```

`STOP` define até onde o script vai antes de deslogar (`calc-window`, `preenche`, `empresas`, `confirma`, `go`). Isso
permite mapear tela por tela sem executar nada de verdade.

```bash
python3 sharepoint/process_prolabore_mes.py --competencia 09/2026 --siglas ABC --dry-run
```
