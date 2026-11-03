#!/usr/bin/env python3
"""Sobe um arquivo para o SharePoint em Publico > Automacao.
Reusa o SharePointClient local (../../sharepoint/sharepoint_client.py, auth por certificado).

Uso: python3 upload_sharepoint.py <arquivo_local> [nome_destino.pdf]
"""
import os
import sys
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "sharepoint"))

# carrega .env.sharepoint (raiz do repo; fallback p/ ~/work/Sharepoint-API)
_env_candidates = [
    REPO_ROOT / ".env.sharepoint",
    pathlib.Path.home() / "work" / "Sharepoint-API" / ".env.sharepoint",
]
_env_file = next((p for p in _env_candidates if p.is_file()), None)
if _env_file is None:
    raise SystemExit(f".env.sharepoint não encontrado em: {', '.join(map(str, _env_candidates))}")
for line in _env_file.read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    os.environ.setdefault(k.strip(), v.strip().strip("'\""))

import sharepoint_client as sp  # noqa: E402

SITE = "https://empresa.sharepoint.com/sites/Empresa"
FOLDER = "/sites/Empresa/Publico/Automacao"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src = pathlib.Path(sys.argv[1]).expanduser()
    if not src.is_file():
        print(f"arquivo não encontrado: {src}")
        return 1
    dest_name = sys.argv[2] if len(sys.argv) > 2 else src.name

    client = sp.SharePointClient(sp.SharePointSettings.from_env())
    # upload_file usa o nome do arquivo local; renomeia se preciso
    if dest_name != src.name:
        tmp = src.with_name(dest_name)
        tmp.write_bytes(src.read_bytes())
        src = tmp
    res = client.upload_file(SITE, FOLDER, src)
    print("OK:", res.get("ServerRelativeUrl"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
