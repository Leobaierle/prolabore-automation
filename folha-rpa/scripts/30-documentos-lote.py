#!/usr/bin/env python3
"""Passos 2+3 em lote: FOPAG + RECPAG de todas as empresas da planilha (1 sessão ERP por empresa).

Roda scripts/11-empresa-folha-recibo.mjs para cada empresa de ~/Empresas.xlsx (Processar=SIM),
em sequência (o usuário do ERP só permite 1 sessão). Retoma de onde parou: pula quem já tem
os 2 PDFs em downloads/. Relatório em downloads/logs/lote-<MM>-<AAAA>.json.

Uso:
  cd folha-rpa
  COMPET_MM=09 COMPET_AAAA=2026 python3 scripts/30-documentos-lote.py [--excluir DEF,XXX] [--only A,B]
"""
import argparse, json, os, subprocess, sys, time
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
DL = ROOT / "downloads"
LOGS = DL / "logs"


def empresas(planilha):
    ws = openpyxl.load_workbook(planilha)["Empresas"]
    for r in ws.iter_rows(min_row=4, values_only=True):
        if r[0] and str(r[2]).strip().upper() == "SIM":
            yield str(r[0]).strip(), str(r[1]).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--planilha", default=str(Path.home() / "Empresas.xlsx"))
    ap.add_argument("--excluir", default="")
    ap.add_argument("--only", default="")
    ap.add_argument("--tentativas", type=int, default=2)
    ap.add_argument("--alias", default="", help="SIGLA_PLANILHA=SIGLA_ERP,... quando a sigla no ERP difere da planilha (ex.: ABC=XYZ)")
    a = ap.parse_args()

    mm = os.environ.get("COMPET_MM", "09")
    aaaa = os.environ.get("COMPET_AAAA", "2026")
    compet = f"{mm} {aaaa}"
    alias = dict(x.split("=") for x in a.alias.split(",") if "=" in x)
    excluir = {x.strip() for x in a.excluir.split(",") if x.strip()}
    only = {x.strip() for x in a.only.split(",") if x.strip()}
    LOGS.mkdir(parents=True, exist_ok=True)
    rel_path = LOGS / f"lote-{mm}-{aaaa}.json"
    rel = json.loads(rel_path.read_text()) if rel_path.exists() else {}

    lista = [(s, n) for s, n in empresas(a.planilha) if s not in excluir and (not only or s in only)]
    print(f"{len(lista)} empresas, competência {compet}", flush=True)
    falhas_seguidas = 0
    for i, (sigla, nome) in enumerate(lista, 1):
        fop = DL / f"{nome} FOPAG - {compet}.pdf"
        rec = DL / f"{nome} RECPAG - {compet}.pdf"
        if fop.exists() and rec.exists() and rel.get(sigla, {}).get("ok"):
            print(f"[{i}/{len(lista)}] {sigla}: já feita, pulando", flush=True)
            continue
        ok = False
        for t in range(1, a.tentativas + 1):
            print(f"[{i}/{len(lista)}] {sigla} ({nome}) tentativa {t}", flush=True)
            env = dict(os.environ, SIGLA=alias.get(sigla, sigla), EMPRESA=nome, COMPET=compet)
            if sigla in ("XYZ",):
                env.pop("EMPRESA_NOME", None)
            t0 = time.time()
            with open(LOGS / f"{sigla}.log", "w") as lf:
                try:
                    p = subprocess.run(["node", "scripts/11-empresa-folha-recibo.mjs"], cwd=ROOT, env=env,
                                       stdout=lf, stderr=subprocess.STDOUT, timeout=1500)
                    rc = p.returncode
                except subprocess.TimeoutExpired:
                    rc = -9
            ok = rc == 0 and fop.exists() and rec.exists()
            rel[sigla] = {"empresa": nome, "ok": ok, "rc": rc, "tentativa": t,
                          "segundos": round(time.time() - t0), "fim": time.strftime("%Y-%m-%d %H:%M:%S")}
            rel_path.write_text(json.dumps(rel, ensure_ascii=False, indent=1))
            print(f"    -> {'OK' if ok else 'FALHOU rc=%s' % rc} ({rel[sigla]['segundos']}s)", flush=True)
            time.sleep(25)       # deixa a sessão do ERP encerrar de vez antes do próximo login
            if ok:
                break
        falhas_seguidas = 0 if ok else falhas_seguidas + 1
        if falhas_seguidas >= 4:
            print("4 falhas seguidas — parando para olhar (provável problema geral).", flush=True)
            sys.exit(2)
    feitas = sum(1 for v in rel.values() if v["ok"])
    print(f"fim: {feitas} ok, {len(lista) - feitas} pendentes/falhas", flush=True)


if __name__ == "__main__":
    main()
