import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { env, login, snap, teardown, trocarEmpresa, DL, ROOT } from './lib.mjs';
const E = env();

const COMPET = process.env.COMPET || '08-2026';   // só p/ nomear o arquivo no SharePoint
const SIGLA = process.env.SIGLA || '';
const EMPRESA_NOME = process.env.EMPRESA_NOME || '';
const EMPRESA = process.env.EMPRESA || SIGLA || EMPRESA_NOME || 'EMPRESA EXEMPLO';

const s = await login(E);
const { page } = s;
try {
  if (!s.ok) throw new Error('login não concluído: ' + s.reason);
  await snap(page, 'main');

  if (SIGLA || EMPRESA_NOME) {
    await trocarEmpresa(page, { sigla: SIGLA, nome: EMPRESA_NOME });
  }

  // 4-Mensais
  await page.mouse.click(505, 89);
  await page.waitForTimeout(2000);
  await snap(page, 'menu-mensais');

  // 1-Folha de Pagamento/13º Salário/Total
  await page.mouse.click(570, 137);
  await page.waitForTimeout(2500);
  await snap(page, 'submenu-1-folha');

  // "L-Listagem de Folhas Calculadas" abre a janela "Seleção - Listagem da folha"
  await page.keyboard.press('l');
  await page.waitForTimeout(3000);
  await snap(page, 'selecao-listagem');

  // Avançar (page down)
  await page.keyboard.press('PageDown');
  await page.waitForTimeout(3000);
  await snap(page, 'apos-avancar-1');

  // janela de confirmação -> Sim
  await page.keyboard.press('Enter');
  await page.waitForTimeout(3000);
  await snap(page, 'config-impressao');

  // Configuração de Impressão: seleciona "F-Gera listagem em PDF"
  await page.mouse.click(545, 362);
  await page.waitForTimeout(1000);
  await snap(page, 'formato-F-pdf');

  // Avançar (page down) -> gera o PDF (possível download do navegador)
  const dlPromise = page.waitForEvent('download', { timeout: 60000 }).catch(() => null);
  await page.keyboard.press('PageDown');
  await page.waitForTimeout(4000);
  await snap(page, 'apos-gerar-1');
  const dl = await dlPromise;
  if (!dl) throw new Error('PDF não gerou download — ver screenshots em artifacts/');
  const local = `${DL}/${dl.suggestedFilename() || 'listagem.pdf'}`;
  await dl.saveAs(local);
  console.log('DOWNLOAD OK ->', local);
  await page.waitForTimeout(2000);
  await snap(page, 'apos-gerar-2');

  // Upload para SharePoint: Publico > Automacao
  const destName = `Listagem Folhas Calculadas - ${EMPRESA} - ${COMPET}.pdf`;
  const out = execFileSync('python3', [
    path.join(ROOT, 'scripts', 'upload_sharepoint.py'), local, destName,
  ], { encoding: 'utf8' });
  console.log('SHAREPOINT', out.trim());
} finally {
  await teardown(s);
}
