// Etapa 3 do pró-labore — envio do eSocial pelo Gerenciador do ERP: S-1200 e, só se
// ele terminar sem erro, S-1210. NUNCA o S-1299 (fechamento), que é outra etapa.
//
// O filtro "0006-PROLABORE" do gerenciador já escopa todos os clientes de pró-labore,
// então não há seleção de empresas. Cada evento abre a sua própria sessão (script 14).
//
// Uso: node scripts/21-etapa-esocial.mjs
// Cada evento sai numa linha "@@ {json}" (lida pelo painel).
import { spawn } from 'node:child_process';
import path from 'node:path';
import { ROOT } from './lib.mjs';

const EVENTOS = ['1200', '1210'];
const emit = (o) => console.log('@@ ' + JSON.stringify(o));

function rodar(env) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [path.join(ROOT, 'scripts', '14-esocial-envio.mjs')], {
      cwd: ROOT,
      env: { ...process.env, ...env },
      stdio: ['ignore', 'inherit', 'inherit'],
    });
    child.on('close', (code) => resolve(code ?? 1));
    child.on('error', () => resolve(1));
  });
}

emit({ tipo: 'inicio', total: EVENTOS.length });
for (const [i, evento] of EVENTOS.entries()) {
  console.log(`\n===== S-${evento} =====`);
  emit({ tipo: 'atual', sigla: `S-${evento}`, nome: `Envio S-${evento}` });
  const code = await rodar({ FILTRO: evento, STOP: 'go' });
  if (code !== 0) {
    emit({ tipo: 'empresa', sigla: `S-${evento}`, nome: `Envio S-${evento}`, status: 'falha', detalhe: `script terminou com código ${code}` });
    for (const resto of EVENTOS.slice(i + 1)) {
      emit({ tipo: 'empresa', sigla: `S-${resto}`, nome: `Envio S-${resto}`, status: 'pulada', detalhe: `não enviado porque o S-${evento} falhou` });
    }
    process.exitCode = 1;
    break;
  }
  emit({
    tipo: 'empresa', sigla: `S-${evento}`, nome: `Envio S-${evento}`, status: 'ok',
    detalhe: 'enviado e lotes verificados — conferir pendências no gerenciador (sem OCR)',
  });
}
