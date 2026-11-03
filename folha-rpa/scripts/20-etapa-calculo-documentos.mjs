// Etapa 1 do pró-labore — por empresa: cálculo do recibo mensal (script 12) e depois
// FOPAG + RECPAG com upload para Publico/Automacao (script 11).
//
// Uma empresa por vez e uma sessão ERP por script (limite de 1 sessão do usuário):
// é o caminho testado de ponta a ponta em 04/09/2026 (XYZ, 08/2026). O cálculo em lote
// de várias empresas numa sessão só (G2/G3 do orquestracao.md) ainda não foi validado.
//
// Uso:
//   COMPET=09/2026 EMPRESAS_JSON='[{"sigla":"XYZ","nome":"Empresa XYZ"}]' \
//     node scripts/20-etapa-calculo-documentos.mjs
//
// ETAPA_STOP_FILE: se esse arquivo existir, para ANTES da próxima empresa (nunca no meio
// de uma sessão ERP). Cada empresa sai numa linha "@@ {json}" (lida pelo painel).
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { DL, ROOT } from './lib.mjs';

const COMPET = process.env.COMPET || '';
const m = COMPET.match(/^(\d{2})\/(\d{4})$/);
if (!m || +m[1] < 1 || +m[1] > 12) {
  console.error(`COMPET inválida: "${COMPET}" (use MM/AAAA)`);
  process.exit(2);
}
const [, MM, AAAA] = m;
const EMPRESAS = JSON.parse(process.env.EMPRESAS_JSON || '[]');
const STOP_FILE = process.env.ETAPA_STOP_FILE || '';

// O ERP abre o cálculo já na competência do período corrente (mês anterior). Nesse caso
// mantém o que ele trouxe (modo validado em produção); outra competência é digitada.
const hoje = new Date();
const anterior = new Date(hoje.getFullYear(), hoje.getMonth() - 1, 1);
const COMPET_MODE =
  process.env.COMPET_MODE ||
  (+MM === anterior.getMonth() + 1 && +AAAA === anterior.getFullYear() ? 'keep' : 'type');

const emit = (o) => console.log('@@ ' + JSON.stringify(o));

function rodar(script, env) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [path.join(ROOT, 'scripts', script)], {
      cwd: ROOT,
      env: { ...process.env, ...env },
      stdio: ['ignore', 'inherit', 'inherit'],
    });
    child.on('close', (code) => resolve(code ?? 1));
    child.on('error', () => resolve(1));
  });
}

fs.mkdirSync(DL, { recursive: true });
emit({ tipo: 'inicio', total: EMPRESAS.length, competencia: COMPET, competMode: COMPET_MODE });

let falhas = 0;
for (const { sigla, nome } of EMPRESAS) {
  if (STOP_FILE && fs.existsSync(STOP_FILE)) {
    emit({ tipo: 'parada', motivo: 'pedido de parada — nenhuma empresa ficou no meio' });
    break;
  }
  console.log(`\n===== ${sigla} — ${nome} =====`);
  emit({ tipo: 'atual', sigla, nome });

  const calc = await rodar('12-calculo-lote.mjs', {
    SIGLAS: sigla, COMPET_MM: MM, COMPET_AAAA: AAAA, COMPET_MODE, STOP: 'go',
  });
  if (calc !== 0) {
    falhas++;
    emit({ tipo: 'empresa', sigla, nome, status: 'falha', detalhe: 'cálculo falhou — documentos não gerados' });
    continue;
  }

  const docs = await rodar('11-empresa-folha-recibo.mjs', {
    SIGLA: sigla, EMPRESA: nome, COMPET: `${MM} ${AAAA}`,
  });
  const faltando = ['FOPAG', 'RECPAG'].filter(
    (t) => !fs.existsSync(path.join(DL, `${nome} ${t} - ${MM} ${AAAA}.pdf`)),
  );
  if (docs !== 0 || faltando.length) {
    falhas++;
    emit({
      tipo: 'empresa', sigla, nome, status: 'falha',
      detalhe: faltando.length ? `calculada, mas sem ${faltando.join(' e ')}` : 'calculada, mas o upload falhou',
    });
    continue;
  }
  emit({ tipo: 'empresa', sigla, nome, status: 'ok', detalhe: 'calculada · FOPAG e RECPAG no Automacao' });
}

process.exitCode = falhas ? 1 : 0;
