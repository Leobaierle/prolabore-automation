#!/usr/bin/env python3
"""Lê as linhas visíveis da grade "Seleção de empresas" do ERP a partir de um PNG (stdin).
Saída JSON: [{y, sigla, filial, marcada}] — só linhas inteiramente visíveis."""
import sys, io, json, subprocess, tempfile, os
from PIL import Image, ImageOps

GRID_Y0, GRID_Y1 = 280, 550     # área útil da grade (linhas ~19px)
_raw = sys.stdin.buffer.read()
try:  # guarda a última tela lida (p/ acompanhar a marcação ao vivo)
    import os
    _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'artifacts', 'ocr-ultima-tela.png')
    with open(_p + '.tmp', 'wb') as _f: _f.write(_raw)
    os.replace(_p + '.tmp', _p)
except Exception:
    pass
im = Image.open(io.BytesIO(_raw)).convert('RGB')
g = im.convert('L').crop((322, GRID_Y0, 372, GRID_Y1)).point(lambda v: 0 if v > 150 else 255)   # texto claro -> preto; vale p/ linha marcada (fundo cinza)
g = g.resize((g.width * 4, g.height * 4), Image.LANCZOS)
with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
    g.save(f.name)
tsv = subprocess.run(['tesseract', f.name, '-', '--psm', '6', '-c',
                      'tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', 'tsv'],
                     capture_output=True, text=True).stdout
os.unlink(f.name)
linhas = {}
for ln in tsv.splitlines()[1:]:
    p = ln.split('\t')
    if len(p) < 12 or not p[11].strip(): continue
    left, top, w, h = (int(p[i]) / 4 for i in (6, 7, 8, 9))
    y = GRID_Y0 + top + h / 2
    key = (int(p[2]), int(p[3]), int(p[4]))          # bloco/par/linha
    linhas.setdefault(key, []).append((left, p[11].strip(), y))
# filial: OCR de 1 char por linha. O '0' volta vazio e os demais dígitos voltam lidos.
def filial_em(y):
    c = im.convert('L').crop((374, int(y) - 8, 398, int(y) + 8)).point(lambda v: 0 if v > 150 else 255)
    c = ImageOps.expand(c.resize((c.width * 8, c.height * 8), Image.LANCZOS), border=40, fill=255)
    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f3:
        c.save(f3.name)
    t = subprocess.run(['tesseract', f3.name, '-', '--psm', '10', '-c', 'tessedit_char_whitelist=0123456789'],
                       capture_output=True, text=True).stdout.strip()
    os.unlink(f3.name)
    return t or '0'
def cnpj_valido(d):
    if len(d) != 14 or len(set(d)) == 1: return False
    for n in (12, 13):
        pesos = ([5,4,3,2,9,8,7,6,5,4,3,2] if n == 12 else [6,5,4,3,2,9,8,7,6,5,4,3,2])
        r = sum(int(x) * p for x, p in zip(d[:n], pesos)) % 11
        if int(d[n]) != (0 if r < 2 else 11 - r): return False
    return True

def cpf_valido(d):
    if len(d) != 11 or len(set(d)) == 1: return False
    for n in (9, 10):
        r = sum(int(d[i]) * (n + 1 - i) for i in range(n)) * 10 % 11 % 10
        if int(d[n]) != r: return False
    return True

# (limiar, escala): linha marcada tem fundo cinza (limiar maior) e o '0' final some em alguns ajustes
COMBOS = [(110, 4), (90, 4), (130, 4), (175, 3), (120, 4), (100, 3), (150, 3), (175, 4), (90, 3), (110, 6), (130, 3), (175, 6)]

def cnpj_em(y):
    """Lê o CNPJ/CPF da linha; só para quando os dígitos verificadores fecham. Se nenhum ajuste
    fechar, devolve o mais longo (o node ainda exige sigla coerente p/ aceitar)."""
    melhor = ''
    for thr, sc in COMBOS:
        c = im.convert('L').crop((864, int(y) - 8, 980, int(y) + 8)).point(lambda v, t=thr: 0 if v > t else 255)
        c = ImageOps.expand(c.resize((c.width * sc, c.height * sc), Image.BICUBIC), border=30, fill=255)
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f4:
            c.save(f4.name)
        t = subprocess.run(['tesseract', f4.name, '-', '--psm', '7', '-c', 'tessedit_char_whitelist=0123456789./-'],
                           capture_output=True, text=True).stdout.strip()
        os.unlink(f4.name)
        d = ''.join(ch for ch in t if ch.isdigit())
        if cnpj_valido(d) or cpf_valido(d): return d
        if len(d) > len(melhor): melhor = d
    return melhor
out = []
for ws in linhas.values():
    ws.sort()
    y = sum(w[2] for w in ws) / len(ws)
    if y < GRID_Y0 + 9 or y > GRID_Y1 - 9: continue   # linha cortada
    sigla = ws[0][1]
    filial = filial_em(y)
    # checkbox: pixels claros (tick) na caixa x 305..318
    bright = 0
    for xx in range(305, 319):
        for yy in range(int(y) - 6, int(y) + 7):
            r, gg, b = im.getpixel((xx, yy))
            if r > 215 and gg > 215 and b > 215: bright += 1
    out.append({'y': round(y), 'sigla': sigla, 'filial': filial, 'marcada': bright >= 6, 'cnpj': cnpj_em(y)})
# linha cuja sigla o OCR não leu (ex.: célula com foco fica branca) deixa um buraco de 2 passos:
# recria a linha pela posição e lê só o resto (CNPJ/checkbox) — a sigla fica '?'
out.sort(key=lambda r: r['y'])
cheio = []
for i, r in enumerate(out):
    if i:
        dy = r['y'] - out[i - 1]['y']
        n = round(dy / 19) - 1
        for k in range(1, n + 1):
            y = out[i - 1]['y'] + round(dy * k / (n + 1))
            if GRID_Y0 + 9 <= y <= GRID_Y1 - 9:
                bright = sum(1 for xx in range(305, 319) for yy in range(y - 6, y + 7)
                             if min(im.getpixel((xx, yy))) > 215)
                cheio.append({'y': y, 'sigla': '?', 'filial': '0', 'marcada': bright >= 6, 'cnpj': cnpj_em(y)})
    cheio.append(r)
print(json.dumps(cheio))
