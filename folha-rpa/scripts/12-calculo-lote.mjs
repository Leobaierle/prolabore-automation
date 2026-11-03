// Passo 1 — Cálculo de Recibo Mensal (lote / múltiplas empresas)
// Menu: 3-Cálculos > 2-Cálculo Mensal/Complementar > C-Cálculo de Recibo Mensal
//
// Uso (teste 1 empresa):
//   SIGLAS=ABC COMPET_MM=09 COMPET_AAAA=2026 STOP=calc-window node scripts/12-calculo-lote.mjs
//
// STOP: até onde ir antes de deslogar (para mapear a tela por screenshot):
//   calc-window  -> abre a janela "Cálculo Mensal" e para
//   preenche     -> preenche competência/data/múltiplas empresas e para
//   func         -> vai até a janela "Seleção padrão - Funcionários" e para
//   empresas     -> vai até a janela com todas as empresas e para
//   confirma     -> marca as empresas, para ANTES do "Sim" final
//   go           -> executa de verdade (confirma o cálculo)
//
// COMPET_MODE: keep (produção, default do ERP já vem certo) | type | posterior/atual/anterior
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import { PNG } from 'pngjs';
const require_crypto = () => crypto;
import { env, login, snap, teardown, pixel, trocarEmpresa, ROOT } from './lib.mjs';

const E = env();
const SIGLAS = (process.env.SIGLAS || 'ABC').split(/[,\s]+/).filter(Boolean);
const COMPET_MM = process.env.COMPET_MM || '09';
const COMPET_AAAA = process.env.COMPET_AAAA || '2026';
const STOP = process.env.STOP || 'calc-window';

let ativaGlobal = '';
const log = (...a) => console.log('[calc]', ...a);
const DPAG = String(new Date(+COMPET_AAAA, +COMPET_MM, 0).getDate()).padStart(2, '0'); // último dia do mês
const COMPET = `${COMPET_MM}/${COMPET_AAAA}`;
const DATA_PAG = `${DPAG}/${COMPET_MM}/${COMPET_AAAA}`;

async function pause(page, ms) { await page.waitForTimeout(ms); }

// digita num campo MASCARADO do canvas (data): seleciona tudo, apaga e digita só os dígitos.
// A máscara (MM/AAAA, DD/MM/AAAA) insere as barras sozinha. Sem espaço-guarda: quebra a máscara.
async function fillMasked(page, x, y, digits) {
  await page.mouse.click(x, y);
  await pause(page, 700);
  await page.keyboard.press('Control+A');
  await pause(page, 200);
  await page.keyboard.press('Delete');
  await pause(page, 250);
  await page.keyboard.press('Home');
  await pause(page, 150);
  for (const d of digits) { await page.keyboard.press(d); await pause(page, 90); }
  await pause(page, 400);
}

// diálogo "Sistema® - Atenção" (bright em ~800,405). Retorna o texto aproximado se aberto.
async function atencaoAberta(page) {
  const p = await pixel(page, 800, 405);
  return p[0] > 200 && p[1] > 200 && p[2] > 200;
}

// diálogo "Sistema® - Processando... aguarde" tem uma faixa azul forte em ~(620,390).
async function processando(page) {
  const p = await pixel(page, 620, 390);
  return p[2] > 150 && p[2] - p[0] > 45 && p[2] - p[1] > 20;
}
// espera o "Processando..." sumir (carga da lista de ~700 empresas pode levar dezenas de s)
async function esperarProcessando(page, maxMs = 90000) {
  const t0 = Date.now();
  await pause(page, 2000);
  while (Date.now() - t0 < maxMs) {
    if (!(await processando(page))) { await pause(page, 1500); if (!(await processando(page))) return true; }
    await pause(page, 2000);
  }
  return false;
}

// janela "Processamento de Múltiplas Empresas": cabeçalho azul em ~(800,285)
async function processamentoAberto(page) {
  const p = await pixel(page, 800, 285);
  return p[2] > 200 && p[2] - p[0] > 120 && p[2] - p[1] > 60;
}

// lê "Concluídas N de M ações" (OCR do trecho)
async function lerProgresso(page) {
  const buf = await page.screenshot({ clip: { x: 520, y: 613, width: 260, height: 18 } });
  const out = execFileSync('python3', ['-c', `
import sys, io, subprocess, tempfile, os
from PIL import Image, ImageOps
im = Image.open(io.BytesIO(sys.stdin.buffer.read())).convert('L').point(lambda v: 0 if v > 110 else 255)
im = ImageOps.expand(im.resize((im.width * 4, im.height * 4), Image.BICUBIC), border=30, fill=255)
f = tempfile.mktemp(suffix='.png'); im.save(f)
print(subprocess.run(['tesseract', f, '-', '--psm', '7'], capture_output=True, text=True).stdout.strip()); os.unlink(f)
`], { input: buf }).toString();
  const mm = out.match(/(\d+)\s*de\s*(\d+)/i);
  return mm ? { n: +mm[1], m: +mm[2], texto: out.trim() } : { n: null, m: null, texto: out.trim() };
}

async function esperarProcessamentoMultiplas(page, maxMs = 60 * 60 * 1000) {
  const t0 = Date.now();
  let ultimo = { n: null, m: null };
  let vistaAberta = false;
  let fechadaSeguidas = 0;
  let ultimoLog = 0;
  while (Date.now() - t0 < maxMs) {
    // empresa única: o ERP não abre a janela de múltiplas empresas (vai direto p/ 'Visualização de recibos')
    if (!vistaAberta && Date.now() - t0 > 45000) { log('janela de múltiplas empresas não apareceu (empresa única?) — seguindo.'); return; }
    // fim do lote: o ERP pergunta "Deseja imprimir a relação de empresas processadas?" (ícone azul ~655,418)
    // e fica parado nela até alguém responder — responde Não (~944,528)
    const q = await pixel(page, 655, 418);
    if (vistaAberta && q[2] > 200 && q[2] - q[0] > 120) {
      log('pergunta "imprimir a relação de empresas processadas?" — respondendo Não');
      await snap(page, 'calc-pergunta-imprimir');
      await page.mouse.click(944, 528);
      await pause(page, 2500);
      continue;
    }
    if (await processamentoAberto(page)) {
      vistaAberta = true; fechadaSeguidas = 0;
      const pr = await lerProgresso(page);
      if (pr.n !== null) ultimo = pr;
      if (Date.now() - ultimoLog > 60000) { log(`processando: ${pr.texto}`); ultimoLog = Date.now(); }
    } else if (vistaAberta && ++fechadaSeguidas >= 3) {
      break;                                 // sumiu e continuou sumida
    }
    await pause(page, 3000);
  }
  if (ultimo.m !== null && ultimo.n !== ultimo.m) {
    // pode ser só a última leitura antes de fechar; o aviso fica no log p/ conferência manual
    log(`ATENÇÃO: última leitura do progresso "${ultimo.n} de ${ultimo.m}" (janela fechou) — conferir os recibos.`);
  } else {
    log(`processamento concluído: ${ultimo.n} de ${ultimo.m} ações.`);
  }
}

// ---- navega o menu até a janela "Cálculo Mensal" ----
async function abrirCalculoMensal(page) {
  await page.mouse.click(433, 89);            // 3-Cálculos
  await pause(page, 2000);
  await snap(page, 'calc-menu-3calculos');

  await page.mouse.click(500, 156);           // 2-Cálculo Mensal/Complementar
  await pause(page, 2500);
  await snap(page, 'calc-submenu-2mensal');

  await page.keyboard.press('c');             // C-Cálculo de Recibo Mensal
  await pause(page, 3500);
  await snap(page, 'calc-janela-calculo-mensal');
}

// janela "Seleção - Cálculo mensal": ajusta competência + data pgto, marca "Múltiplas empresas".
// COMPET_MODE: 'type' (digita MMAAAA) | 'keep' (não mexe — produção, default já vem certo)
//              | 'posterior'/'atual'/'anterior' (tecla P/H/A no campo)
async function preencherCalculoMensal(page) {
  const mode = process.env.COMPET_MODE || 'type';
  if (mode === 'keep') {
    log('COMPET_MODE=keep — mantendo a competência que o ERP trouxe.');
  } else if (mode === 'type') {
    await fillMasked(page, 745, 424, `${COMPET_MM}${COMPET_AAAA}`);   // Competência MM/AAAA
    await snap(page, 'calc-competencia');
    if (await atencaoAberta(page)) throw new Error('Competência recusada ("Mês/ano inválido"?) — ver artifacts/calc-competencia');
  } else {
    const key = { posterior: 'p', atual: 'h', anterior: 'a' }[mode];
    await page.mouse.click(745, 424);
    await pause(page, 500);
    await page.keyboard.press(key.toUpperCase());
    await pause(page, 800);
    await snap(page, 'calc-competencia');
  }

  // Data de pagamento — normalmente o ERP reajusta sozinho ao trocar a competência.
  // Só digita se DATA_PAG_SET=1.
  if (process.env.DATA_PAG_SET === '1') {
    await fillMasked(page, 750, 446, `${DPAG}${COMPET_MM}${COMPET_AAAA}`);
    await snap(page, 'calc-data-pgto');
    if (await atencaoAberta(page)) throw new Error('Data de pagamento recusada — ver artifacts/calc-data-pgto');
  }

  // checkbox "Múltiplas empresas" (~717,491)
  await page.mouse.click(717, 491);
  await pause(page, 1200);
  await snap(page, 'calc-multiplas-empresas');
  if (await atencaoAberta(page)) throw new Error('Diálogo de atenção após marcar "Múltiplas empresas" — ver artifacts/');
}

const s = await login(E);
const { page } = s;
try {
  await run();
} catch (e) {
  log('=== FALHA:', String(e).slice(0, 200));
  process.exitCode = 1;
} finally {
  await teardown(s);
}

// janela "Seleção de empresas" (versão atual): a caixa de busca não é confiável (some, e falha
// p/ siglas de 2 letras). Em vez dela, VARRE a grade inteira por rolagem (ordenada por nome), lê
// a sigla de cada linha por OCR (scripts/ocr_linhas.py) e clica no checkbox das siglas-alvo.
// A empresa ATIVA já vem marcada e travada — por isso fica FORA de SIGLAS.
// (function declarations: o run() é chamado antes destas linhas serem avaliadas — const cairia na TDZ)
function norm(x) { return x.toUpperCase().replace(/0/g, 'O').replace(/1/g, 'I').replace(/[35]/g, 'S').replace(/8/g, 'B').replace(/2/g, 'Z'); }

async function pixelHash(page) {          // assinatura barata da região da grade (sem OCR)
  const buf = await page.screenshot({ clip: { x: 300, y: 280, width: 1000, height: 270 } });
  return require_crypto().createHash('md5').update(buf).digest('hex');
}

async function lerLinhas(page) {
  const buf = await page.screenshot();
  if (process.env.DUMP_DIR) fs.writeFileSync(path.join(process.env.DUMP_DIR, `p${Date.now()}.png`), buf);
  const out = execFileSync('python3', [path.join(ROOT, 'scripts', 'ocr_linhas.py')], { input: buf, maxBuffer: 1 << 24 });
  return JSON.parse(out.toString());
}

// rola do topo ao fim; chama onPage(linhas) em cada "tela". Para quando a tela não muda mais.
async function varrerLista(page, onPage) {
  // volta ao topo: uma roda gigante não funciona no Webswing — rola em passos até a tela parar de mudar
  await page.mouse.move(700, 420);
  let topoAnt = '';
  for (let i = 0; i < 120; i++) {
    await page.mouse.wheel(0, -1500);
    await pause(page, 700);
    const sig = (await pixelHash(page));
    if (sig === topoAnt) break;
    topoAnt = sig;
  }
  await pause(page, 800);
  let anterior = '';
  let prevSet = null;
  let repetidas = 0;
  let passo = 800;                        // px de roda por passo; ajusta sozinho p/ manter sobreposição
  for (let i = 0; i < 150; i++) {
    if (process.env.MAX_TELAS && i >= +process.env.MAX_TELAS) return;   // só p/ diagnóstico
    const linhas = await lerLinhas(page);
    const assinatura = linhas.map((l) => l.cnpj).join('|');
    if (assinatura === anterior) { if (++repetidas >= 3) return; await page.mouse.wheel(0, passo); await pause(page, 1200); continue; }
    repetidas = 0;
    const atual = new Set(linhas.map((l) => l.cnpj));
    if (prevSet && ![...atual].some((x) => prevSet.has(x)) && passo > 100) {
      // pulou linhas (sem sobreposição): volta meio passo e reduz
      await page.mouse.wheel(0, -passo / 2);
      passo = Math.round(passo / 2);
      await pause(page, 900);
      continue;
    }
    anterior = assinatura;
    prevSet = atual;
    if (process.env.DEBUG_OCR) fs.appendFileSync(process.env.DEBUG_OCR, JSON.stringify({ i, linhas: linhas.map((l) => `${l.y}:${l.sigla}/${l.cnpj}${l.marcada ? "*" : ""}`) }) + '\n');
    if (process.env.SNAP_TELAS) await snap(page, `varredura-${i}`);
    await onPage(linhas, i);
    await page.mouse.move(700, 420);
    await page.mouse.wheel(0, passo);
    await pause(page, 900);
  }
  throw new Error('varredura da lista não terminou em 150 telas');
}

// A chave de busca é o CNPJ (coluna da grade, dígitos lidos por OCR com boa precisão), vindo da
// planilha Empresas.xlsx via env CNPJS="SIGLA=cnpj,SIGLA=cnpj". A sigla lida por OCR é ruidosa
// (J→I, N→M) e só serve de log — nunca decide qual empresa marcar.
function cnpjsAlvo() {
  const m = new Map();
  for (const par of (process.env.CNPJS || '').split(',').filter(Boolean)) {
    const [sig, cnpj] = par.split('=');
    m.set(sig, (cnpj || '').replace(/\D/g, '').padStart(14, '0'));
  }
  return m;
}

function dist(a, b) {                       // Levenshtein
  const d = Array.from({ length: a.length + 1 }, (_, i) => [i, ...Array(b.length).fill(0)]);
  for (let j = 1; j <= b.length; j++) d[0][j] = j;
  for (let i = 1; i <= a.length; i++) for (let j = 1; j <= b.length; j++)
    d[i][j] = Math.min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
  return d[a.length][b.length];
}

// Qual alvo (cnpj) esta linha da grade representa? 1º CNPJ idêntico; senão, OCR com 1-2 dígitos
// errados/perdidos: aceita só se o CNPJ está a ≤2 edições E a sigla lida a ≤1 do alvo (dois sinais
// independentes) E o candidato é único. Caso contrário, null.
function casar(l, alvo /* Map cnpj -> sigla */) {
  if (alvo.has(l.cnpj)) return l.cnpj;
  if (l.cnpj.length < 11) return null;
  const c = [...alvo].filter(([cn, sig]) => dist(l.cnpj, cn) <= 2 && dist(norm(l.sigla), norm(sig)) <= 1);
  return c.length === 1 ? c[0][0] : null;
}

// confere o checkbox da linha por pixel (tick branco) numa captura nova
async function checkboxMarcado(page, y) {
  const png = PNG.sync.read(await page.screenshot());
  let claros = 0;
  for (let x = 305; x <= 318; x++) for (let yy = y - 6; yy <= y + 6; yy++) {
    const i = (png.width * yy + x) * 4;
    if (png.data[i] > 215 && png.data[i + 1] > 215 && png.data[i + 2] > 215) claros++;
  }
  return claros >= 6;
}

// contador "Marcados N de M" no canto da janela (OCR de um trecho pequeno)
async function lerContador(page) {
  const buf = await page.screenshot({ clip: { x: 1195, y: 241, width: 110, height: 16 } });
  const out = execFileSync('python3', ['-c', `
import sys, io, subprocess, tempfile, os
from PIL import Image, ImageOps
im = Image.open(io.BytesIO(sys.stdin.buffer.read())).convert('L').point(lambda v: 0 if v > 110 else 255)
im = ImageOps.expand(im.resize((im.width * 5, im.height * 5), Image.BICUBIC), border=30, fill=255)
f = tempfile.mktemp(suffix='.png'); im.save(f)
print(subprocess.run(['tesseract', f, '-', '--psm', '7'], capture_output=True, text=True).stdout.strip()); os.unlink(f)
`], { input: buf }).toString();
  const mm = out.match(/(\d+)\s*de\s*(\d+)/i);
  return { texto: out.trim(), marcados: mm ? +mm[1] : null, total: mm ? +mm[2] : null };
}

async function marcarEmpresas(page) {
  const cnpjs = cnpjsAlvo();
  const semCnpj = SIGLAS.filter((x) => !cnpjs.has(x));
  if (semCnpj.length) throw new Error(`sem CNPJ na planilha para: ${semCnpj.join(',')}`);
  const alvo = new Map(SIGLAS.map((x) => [cnpjs.get(x), x]));   // cnpj -> sigla
  const feitas = new Set();
  const falhasClique = new Map();
  await varrerLista(page, async (linhas, n) => {
    for (const l of linhas) {
      const k = casar(l, alvo);
      if (!k || feitas.has(k)) continue;
      if (!l.marcada) {
        let ok = false;
        for (let t = 0; t < 3 && !ok; t++) {
          if (t === 0 || !(await checkboxMarcado(page, l.y))) {   // re-clica só se, após esperar, continua desmarcada
            await page.mouse.click(311, l.y);
          }
          for (let w = 0; w < 4 && !ok; w++) { await pause(page, 600); ok = await checkboxMarcado(page, l.y); }
        }
        if (!ok) {
          // clique na borda da grade às vezes não pega: não aborta — a linha reaparece na próxima
          // tela (rolagem com sobreposição) e é retentada lá. Se nunca marcar, o "faltam" e o
          // contador abaixo barram antes do "Sim".
          const f = (falhasClique.get(k) || 0) + 1; falhasClique.set(k, f);
          await snap(page, `falha-clique-${alvo.get(k)}-${f}`);
          log(`clique não marcou ${alvo.get(k)} (y=${l.y}, tentativa ${f}) — retento na próxima tela`);
          if (f >= 4) throw new Error(`clique não marcou ${alvo.get(k)} após ${f} telas`);
          continue;
        }
      }
      feitas.add(k);
      log(`marcada ${alvo.get(k)} (lida "${l.sigla}" ${l.cnpj}, tela ${n}, y=${l.y})`);
    }
  });
  const faltam = [...alvo].filter(([k]) => !feitas.has(k)).map(([, v]) => v);
  if (faltam.length) throw new Error(`não achei/marquei na lista: ${faltam.join(',')}`);

  // verificação: o contador da própria janela ("Marcados N de M") tem de ser alvos + ativa
  await page.mouse.move(700, 420);
  for (let i = 0; i < 120; i++) { await page.mouse.wheel(0, -1500); await pause(page, 150); }
  await pause(page, 800);
  const esperado = alvo.size + (cnpjs.get(ativaGlobal) && !alvo.has(cnpjs.get(ativaGlobal)) ? 1 : 0);
  const c = await lerContador(page);
  log(`contador do ERP: "${c.texto}" — esperado ${esperado}`);
  await snap(page, 'calc-contador');
  if (c.marcados !== esperado) throw new Error(`contador diverge: ERP=${c.marcados} esperado=${esperado}`);
  await snap(page, 'calc-empresas-marcadas');
}

async function run() {
  if (!s.ok) throw new Error('login não concluído: ' + s.reason);
  await snap(page, 'calc-main');
  log('competência alvo:', COMPET, '| data pgto:', DATA_PAG, '| siglas:', SIGLAS.join(','));

  // Troca a empresa ativa para a 1ª sigla da lista: assim ELA entra pré-marcada/travada
  // na janela de seleção e o cálculo não arrasta a empresa que estava aberta antes.
  const ativa = process.env.EMPRESA_ATIVA || SIGLAS[0];
  ativaGlobal = ativa;
  const restantes = SIGLAS.filter((x) => x !== ativa);
  await trocarEmpresa(page, { sigla: ativa });
  for (let i = 0; i < 6; i++) { await page.keyboard.press('Escape').catch(() => {}); await pause(page, 250); }
  log(`empresa ativa -> ${ativa}; a marcar no filtro: ${restantes.join(',') || '(nenhuma — só a ativa)'}`);

  await abrirCalculoMensal(page);
  if (STOP === 'calc-window') { log('STOP=calc-window'); return; }

  await preencherCalculoMensal(page);
  if (STOP === 'preenche') { log('STOP=preenche'); return; }

  // Avançar (janela "Cálculo mensal") -> janela "Seleção padrão - Funcionários"
  await page.keyboard.press('PageDown');
  await pause(page, 4500);
  await snap(page, 'calc-selecao-funcionarios');
  if (STOP === 'func') { log('STOP=func'); return; }

  // Avançar (Funcionários) -> janela com TODAS as empresas (carga ~700 registros).
  await page.keyboard.press('PageDown');   // atalho do Avançar; a posição do botão varia com a empresa (linha "Código da filial")
  await pause(page, 3000);
  const ok = await esperarProcessando(page);
  log(ok ? 'lista de empresas carregada.' : 'ATENÇÃO: "Processando" não sumiu no tempo limite.');
  await pause(page, 2000);
  await snap(page, 'calc-janela-empresas');
  if (STOP === 'empresas') { log('STOP=empresas'); return; }
  if (process.env.SCAN_ONLY) { await varrerLista(page, async () => {}); return; }

  // marca as siglas restantes (a ativa já está marcada/travada)
  const SIGLAS_ORIG = SIGLAS.slice();
  SIGLAS.length = 0; SIGLAS.push(...restantes);
  if (restantes.length) await marcarEmpresas(page);
  SIGLAS.length = 0; SIGLAS.push(...SIGLAS_ORIG);
  await snap(page, 'calc-antes-confirmar');
  if (STOP === 'confirma') { log('STOP=confirma — NÃO confirmei o cálculo.'); return; }

  // Avançar -> diálogo de confirmação -> Sim
  await page.mouse.click(1247, 720);           // Avançar (page down)
  await pause(page, 3500);
  await snap(page, 'calc-confirmacao');
  await page.keyboard.press('Enter');          // "Confirma?" -> Sim
  await pause(page, 4000);
  await snap(page, 'calc-apos-sim');

  // processamento do cálculo: no versão atual abre a janela "Processamento de Múltiplas Empresas"
  // (cabeçalho azul + barra + "Concluídas N de M ações"). Espera ela SUMIR e confere N == M.
  // NUNCA seguir p/ a limpeza/logout com ela aberta: Escape/clique ali dispara "Interromper processamento?".
  await esperarProcessamentoMultiplas(page);
  await pause(page, 3000);
  await snap(page, 'calc-visualizacao-recibos');

  // janela "Visualização de recibos processados": NÃO fecha com Escape.
  // Fecha pelo botão "Sair" (~1176,731) e trata o eventual aviso de "redimensionar
  // janela" (Atenção com OK ~1037,562).
  for (let tent = 0; tent < 4; tent++) {
    await page.mouse.click(1176, 731).catch(() => {});          // "Sair"
    await pause(page, 1500);
    if (await atencaoAberta(page)) {                            // aviso de redimensionamento
      await page.mouse.click(1037, 562).catch(() => {});        // OK
      await pause(page, 1000);
      await page.keyboard.press('Enter').catch(() => {});
      await pause(page, 1000);
    }
    await page.mouse.click(1224, 194).catch(() => {});          // X da janela (fallback)
    await pause(page, 1200);
    for (let i = 0; i < 4; i++) { await page.keyboard.press('Escape').catch(() => {}); await pause(page, 350); }
    // tela principal limpa? pixel escuro no meio-alto onde a janela ficava
    const p = await pixel(page, 780, 400);
    if (p[0] < 70 && p[1] < 70 && p[2] < 80) break;
  }
  await snap(page, 'calc-fim');
  log('=== CÁLCULO CONCLUÍDO (siglas: ' + SIGLAS.join(',') + ', ativa: ' + ativa + ') ===');
}
