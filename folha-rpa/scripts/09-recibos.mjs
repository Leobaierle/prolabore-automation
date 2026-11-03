import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { env, login, snap, teardown, trocarEmpresa, DL, ROOT } from './lib.mjs';
const E = env();

const COMPET = process.env.COMPET || '08-2026';
const SIGLA = process.env.SIGLA || '';
const EMPRESA_NOME = process.env.EMPRESA_NOME || '';
const EMPRESA = process.env.EMPRESA || SIGLA || EMPRESA_NOME || 'EMPRESA EXEMPLO';
const EXPLORE = process.env.EXPLORE === '1';

const s = await login(E);
const { page } = s;
try {
  if (!s.ok) throw new Error('login não concluído: ' + s.reason);
  await snap(page, 'main');

  if (SIGLA || EMPRESA_NOME) {
    await trocarEmpresa(page, { sigla: SIGLA, nome: EMPRESA_NOME });
  }

  // 3-Cálculos
  await page.mouse.click(433, 89);
  await page.waitForTimeout(2000);
  await snap(page, 'menu-calculos');

  if (!EXPLORE) {
    // 2-Cálculo Mensal/Complementar
    await page.mouse.click(500, 156);
    await page.waitForTimeout(2500);
    await snap(page, 'submenu-2-calculo');

    // L-Listagem de Recibos Mensais/Complementares
    await page.keyboard.press('l');
    await page.waitForTimeout(3000);
    await snap(page, 'janela-1');

    await page.keyboard.press('PageDown');   // avançar
    await page.waitForTimeout(3000);
    await snap(page, 'janela-2');

    await page.keyboard.press('PageDown');   // avançar
    await page.waitForTimeout(3000);
    await snap(page, 'janela-confirma');

    await page.keyboard.press('Enter');      // confirma -> Sim
    await page.waitForTimeout(3000);
    await snap(page, 'janela-visualizar');

    // "visualizar os recibos em tela antes de imprimir?" -> Sim (+ captura download)
    const dlPromise = page.waitForEvent('download', { timeout: 90000 }).catch(() => null);
    await page.keyboard.press('Enter');
    await page.waitForTimeout(5000);
    await snap(page, 'apos-visualizar');

    const dl = await dlPromise;
    if (!dl) throw new Error('RECPAG não gerou download — ver screenshots');
    // o visualizador Webswing serve o PDF como blob (nome = uuid); renomeia
    const local = `${DL}/Recibos Mensais - ${EMPRESA} - ${COMPET}.pdf`;
    await dl.saveAs(local);
    console.log('DOWNLOAD OK ->', local);
    await snap(page, 'apos-download');

    const out = execFileSync('python3', [
      path.join(ROOT, 'scripts', 'upload_sharepoint.py'), local, path.basename(local),
    ], { encoding: 'utf8' });
    console.log('SHAREPOINT', out.trim());
  }
} finally {
  await teardown(s);
}
