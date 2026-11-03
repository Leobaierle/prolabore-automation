// Fluxo completo numa única sessão ERP:
//   login -> trocarEmpresa(SIGLA) -> gera FOPAG (folha) -> gera RECPAG (recibo)
//   -> upload dos 2 PDFs para SharePoint (Publico/Automacao) -> logout verificado.
//
// Uso:
//   SIGLA=3PO EMPRESA="3 Polettos" COMPET="08 2026" node scripts/11-empresa-folha-recibo.mjs
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { env, login, snap, teardown, trocarEmpresa, pixel, DL, ROOT } from './lib.mjs';

const E = env();
const SIGLA = process.env.SIGLA || '';
const EMPRESA_NOME = process.env.EMPRESA_NOME || '';
const EMPRESA = process.env.EMPRESA || SIGLA || EMPRESA_NOME || 'EMPRESA EXEMPLO';
const COMPET = process.env.COMPET || '08 2026';   // usado só para nomear o arquivo
const SKIP_FOPAG = process.env.SKIP_FOPAG === '1';   // pula a folha (já gerada/enviada)
const SKIP_RECPAG = process.env.SKIP_RECPAG === '1'; // pula o recibo

// Quando a janela "Configuração de Impressão" aparece no RECPAG (só às vezes),
// qual opção de "Tipo de impressão" selecionar. Default: P-Impr.gráfica com visualização.
// (a lista do RECPAG tem 7 itens: V/G/P/S/F/R/N — y aproximado de cada linha)
const RECPAG_PRINT_OPT = (process.env.RECPAG_PRINT_OPT || 'P').toUpperCase();
const RECPAG_OPT_Y = { V: 335, G: 349, P: 362, S: 376, F: 390, R: 404, N: 418 };

function upload(local, destName) {
  const out = execFileSync('python3', [
    path.join(ROOT, 'scripts', 'upload_sharepoint.py'), local, destName,
  ], { encoding: 'utf8' });
  console.log('SHAREPOINT', out.trim());
}

async function fecharTudo(page) {
  for (let i = 0; i < 6; i++) { await page.keyboard.press('Escape').catch(() => {}); await page.waitForTimeout(300); }
  await page.waitForTimeout(1000);
}

// ---- Passo 3 doc: FPA 4.1-L "Listagem de Folhas Calculadas" -> PDF ----
async function gerarFolha(page) {
  await page.mouse.click(505, 89);            // 4-Mensais
  await page.waitForTimeout(2000);
  await snap(page, 'fopag-menu-mensais');

  await page.mouse.click(570, 137);           // 1-Folha de Pagamento/13º/Total
  await page.waitForTimeout(2500);
  await snap(page, 'fopag-submenu');

  await page.keyboard.press('l');             // L-Listagem de Folhas Calculadas
  await page.waitForTimeout(3000);
  await snap(page, 'fopag-selecao');

  await page.keyboard.press('PageDown');      // avançar
  await page.waitForTimeout(3000);
  await snap(page, 'fopag-apos-avancar');

  await page.keyboard.press('Enter');         // "Confirma?" -> Sim
  await page.waitForTimeout(3000);
  await snap(page, 'fopag-config-impressao');

  await page.mouse.click(545, 362);           // F-Gera listagem em PDF
  await page.waitForTimeout(1000);
  await snap(page, 'fopag-formato-F');

  const dlPromise = page.waitForEvent('download', { timeout: 60000 }).catch(() => null);
  await page.keyboard.press('PageDown');      // gera o PDF
  await page.waitForTimeout(4000);
  await snap(page, 'fopag-apos-gerar');
  const dl = await dlPromise;
  if (!dl) throw new Error('FOPAG: PDF não gerou download — ver artifacts/');
  const local = `${DL}/${EMPRESA} FOPAG - ${COMPET}.pdf`;
  await dl.saveAs(local);
  console.log('FOPAG DOWNLOAD OK ->', local);
  await page.waitForTimeout(2000);
  return local;
}

// ---- Passo 2 doc: FPA 3.2-L "Listagem de Recibos Mensais/Complementares" -> PDF ----
async function gerarRecibo(page) {
  await page.mouse.click(433, 89);            // 3-Cálculos
  await page.waitForTimeout(2000);
  await snap(page, 'recpag-menu-calculos');

  await page.mouse.click(500, 156);           // 2-Cálculo Mensal/Complementar
  await page.waitForTimeout(2500);
  await snap(page, 'recpag-submenu');

  await page.keyboard.press('l');             // L-Listagem de Recibos Mensais/Complementares
  await page.waitForTimeout(3000);
  await snap(page, 'recpag-janela-1');

  await page.keyboard.press('PageDown');      // avançar
  await page.waitForTimeout(3000);
  await snap(page, 'recpag-janela-2');

  await page.keyboard.press('PageDown');      // avançar
  await page.waitForTimeout(3000);
  await snap(page, 'recpag-confirma');

  await page.keyboard.press('Enter');         // "Confirma?" -> Sim
  await page.waitForTimeout(3000);
  await snap(page, 'recpag-visualizar');

  // Depois do "Confirma?" o ERP pode seguir por dois caminhos:
  //  (a) diálogo "Visualizar os recibos em tela antes de imprimir?" -> Enter (Sim) gera;
  //  (b) janela "Configuração de Impressão" -> selecionar RECPAG_PRINT_OPT
  //      (default P-Impr.gráfica com visualização) e PageDown (Avançar).
  let done = false;
  const dlPromise = page.waitForEvent('download', { timeout: 120000 })
    .then((d) => { done = true; return d; })
    .catch(() => null);
  await page.keyboard.press('Enter');         // caminho (a)
  await page.waitForTimeout(5000);
  await snap(page, 'recpag-apos-enter');
  if (!done) {                                // caminho (b): "Configuração de Impressão"
    // faixa azul do título da janela em ~(600,270) enquanto ela existe
    const t = await pixel(page, 600, 270);
    const configImpressao = t[2] > 150 && t[2] - t[0] > 40;
    if (configImpressao) {
      const y = RECPAG_OPT_Y[RECPAG_PRINT_OPT] ?? RECPAG_OPT_Y.P;
      console.log(`RECPAG: "Configuração de Impressão" -> opção ${RECPAG_PRINT_OPT} (y=${y})`);
      await page.mouse.click(545, y);         // seleciona o Tipo de impressão pedido
      await page.waitForTimeout(1000);
      await snap(page, 'recpag-opt-selecionada');
    }
    await page.keyboard.press('PageDown');    // Avançar
    await page.waitForTimeout(6000);
    // "P-Impr.gráfica com visualização" abre um preview; pode pedir mais um Avançar/Enter
    if (!done) { await page.keyboard.press('Enter'); await page.waitForTimeout(4000); }
  }
  await snap(page, 'recpag-apos-visualizar');
  const dl = await dlPromise;
  if (!dl) throw new Error('RECPAG: PDF não gerou download — ver artifacts/');
  const local = `${DL}/${EMPRESA} RECPAG - ${COMPET}.pdf`;
  await dl.saveAs(local);
  console.log('RECPAG DOWNLOAD OK ->', local);
  await page.waitForTimeout(2000);
  return local;
}

const s = await login(E);
const { page } = s;
let fopag = null;
let recpag = null;
try {
  if (!s.ok) throw new Error('login não concluído: ' + s.reason);
  await snap(page, 'main');

  if (SIGLA || EMPRESA_NOME) {
    await trocarEmpresa(page, { sigla: SIGLA, nome: EMPRESA_NOME });
  }
  await fecharTudo(page);                 // garante tela principal limpa
  await snap(page, 'pos-troca-limpo');

  if (!SKIP_FOPAG) {
    fopag = await gerarFolha(page);
    await fecharTudo(page);
  } else {
    console.log('SKIP_FOPAG=1 — pulando a folha');
  }
  if (!SKIP_RECPAG) {
    recpag = await gerarRecibo(page);
    await fecharTudo(page);
  } else {
    console.log('SKIP_RECPAG=1 — pulando o recibo');
  }

  // uploads só depois de ter os PDFs em mãos
  if (fopag) upload(fopag, path.basename(fopag));
  if (recpag) upload(recpag, path.basename(recpag));
  console.log('\n=== OK: documentos gerados e enviados para Publico/Automacao ===');
} catch (e) {
  console.log('\n=== FALHA:', String(e).slice(0, 200));
  // se um dos PDFs baixou antes da falha, ainda tenta subir o que tem
  try { if (fopag) upload(fopag, path.basename(fopag)); } catch {}
  try { if (recpag) upload(recpag, path.basename(recpag)); } catch {}
  process.exitCode = 1;
} finally {
  await teardown(s);
}
