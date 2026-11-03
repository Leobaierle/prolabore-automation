// Envio de eventos do eSocial (Gerenciador de Envio) — 1200 -> 1210 -> (NUNCA 1299)
// Uso: FILTRO=1210 STOP=marcado node scripts/14-esocial-envio.mjs
//   STOP: menu8 | sub1 | gerenciador | busca | marcado | enviar1 | verificar1 | go
import fs from 'node:fs';
import path from 'node:path';
import { env, login, snap, teardown, trocarEmpresa, pixel, ROOT } from './lib.mjs';

const E = env();
const SIGLA = process.env.SIGLA || 'XYZ';
const FILTRO = process.env.FILTRO; // ex: '1210' — obrigatório pra ir além de 'gerenciador'
const STOP = process.env.STOP || 'gerenciador';
const log = (...a) => console.log('[esocial]', ...a);

// política de reverificação (04/09/2026): verificar lotes -> se sobrou algo,
// esperar 1min e verificar de novo -> se ainda sobrou, esperar 3min e verificar mais uma vez.
// Não temos OCR pra contar automaticamente quantos sobraram: cada rodada tira uma screenshot
// pra conferência visual (humana, por ora — integração prevista isso a um portal depois).
async function limparBusca(page) {
  await page.mouse.click(290, 699); // botão "x" de limpar busca
  await page.waitForTimeout(500);
}
async function buscar(page, texto) {
  await limparBusca(page);
  await page.mouse.click(160, 699);
  await page.waitForTimeout(300);
  await page.keyboard.type(texto);
  await page.waitForTimeout(1200);
}
async function verificarLotesComRetry(page, filtro) {
  const esperas = [0, 60000, 180000]; // imediato, depois 1min, depois 3min
  for (let i = 0; i < esperas.length; i++) {
    if (esperas[i] > 0) {
      log(`aguardando ${esperas[i] / 1000}s antes de reverificar lotes (tentativa ${i + 1})...`);
      await page.waitForTimeout(esperas[i]);
    }
    await page.mouse.click(548, 750); // Verificar lotes
    await page.waitForTimeout(1500);
    await page.mouse.click(710, 769); // Sim
    log(`"Verificar lotes" (tentativa ${i + 1}) — aguardando concluir...`);
    await esperarDialogoSumir(page, 120000);
    await page.waitForTimeout(2000);
    await buscar(page, filtro);
    await snap(page, `retry${i + 1}-${filtro}-restantes`);
    log(`tentativa ${i + 1}: screenshot dos restantes com filtro "${filtro}" tirada — conferir visualmente.`);
  }
  // isola quem ficou pra trás com "Envio impedido pelo ERP" pra futura integração c/ portal
  await buscar(page, 'Impedido pelo ERP');
  await snap(page, `${filtro}-impedidos-pelo-erp`);
  const notaPath = path.join(ROOT, `esocial_${filtro}_impedidos_${Date.now()}.json`);
  fs.writeFileSync(notaPath, JSON.stringify({
    filtro, quando: new Date().toISOString(),
    obs: 'Conferir screenshot artifacts/*-impedidos-pelo-erp.png — sem OCR ainda, checagem manual. Reportar no portal quando integrado.',
  }, null, 2));
  log('nota de impedidos salva em', notaPath);
}

// diálogo com faixa azul forte (mesmo padrão do "Troca de empresa" / "Envio ao eSocial")
async function dialogoAzulAberto(page) {
  const p = await pixel(page, 700, 330);
  return p[2] > 140 && p[2] - p[0] > 55 && p[2] - p[1] > 25;
}
async function esperarDialogoSumir(page, maxMs = 300000) {
  const t0 = Date.now();
  await page.waitForTimeout(3000);
  while (Date.now() - t0 < maxMs) {
    if (!(await dialogoAzulAberto(page))) {
      await page.waitForTimeout(2000);
      if (!(await dialogoAzulAberto(page))) return true;
    }
    await page.waitForTimeout(4000);
  }
  return false;
}

const s = await login(E);
const { page } = s;
try {
  await trocarEmpresa(page, { sigla: SIGLA });
  log('empresa trocada ->', SIGLA);

  await page.mouse.click(827, 89); // 8-Esocial
  await page.waitForTimeout(1200);
  if (STOP === 'menu8') { log('STOP=menu8'); }
  else {
    await page.mouse.click(862, 137); // 1-Rotinas de Envio
    await page.waitForTimeout(1200);
    if (STOP === 'sub1') { log('STOP=sub1'); }
    else {
      await page.mouse.click(1060, 232); // V-Gerenciador de Envio do eSocial
      log('aguardando carregar (20s)...');
      await page.waitForTimeout(20000);
      await snap(page, 'gerenciador-carregado');
      if (STOP === 'gerenciador' || !FILTRO) { log('STOP=gerenciador (ou sem FILTRO definido)'); }
      else {
        await page.mouse.click(160, 699); // campo de busca
        await page.waitForTimeout(400);
        await page.keyboard.type(FILTRO);
        await page.waitForTimeout(1500);
        await snap(page, `busca-${FILTRO}`);
        log('busca digitada:', FILTRO);
        if (STOP === 'busca') { log('STOP=busca'); }
        else {
          await page.mouse.click(30, 143); // checkbox cabeçalho — marca todos os filtrados
          await page.waitForTimeout(1500);
          await snap(page, `marcado-${FILTRO}`);
          log(`marcados os filtrados por "${FILTRO}" — CONFERIR screenshot antes de enviar!`);
          if (STOP === 'marcado') {
            log('STOP=marcado — rolando a grade pra conferir todas as linhas marcadas.');
            for (let i = 1; i <= 9; i++) { await page.mouse.wheel(0, 1500); await page.waitForTimeout(400); }
            for (let i = 1; i <= 4; i++) {
              await page.mouse.wheel(0, 1500);
              await page.waitForTimeout(700);
              await snap(page, `marcado-${FILTRO}-fim${i}`);
            }
          }
          else {
            await page.mouse.click(398, 750); // Enviar pendências
            await page.waitForTimeout(1500);
            await snap(page, 'enviar-dialogo');
            log('"Enviar pendências" clicado.');
            if (STOP === 'enviar1') { log('STOP=enviar1'); }
            else {
              await page.mouse.click(710, 769); // Sim
              log('Sim clicado — aguardando conclusão do envio (até 5min)...');
              const ok = await esperarDialogoSumir(page);
              log('diálogo de envio sumiu?', ok);
              await snap(page, 'apos-envio-completo');

              log('aguardando 10s antes de Verificar lotes...');
              await page.waitForTimeout(10000);
              if (STOP === 'verificar1') {
                await page.mouse.click(548, 750); // Verificar lotes
                await page.waitForTimeout(1500);
                await snap(page, 'verificar-dialogo');
                log('STOP=verificar1 — parando só no diálogo, sem confirmar.');
              } else {
                await verificarLotesComRetry(page, FILTRO);
                log('verificação com retry concluída.');
              }
            }
          }
        }
      }
    }
  }
} finally {
  await teardown(s);
}
