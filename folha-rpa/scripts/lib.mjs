import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { PNG } from 'pngjs';

// Lê o RGB de um pixel do screenshot atual (para detectar estado da tela canvas).
export async function pixel(page, x, y) {
  const buf = await page.screenshot();
  const png = PNG.sync.read(buf);
  const i = (png.width * y + x) * 4;
  return [png.data[i], png.data[i + 1], png.data[i + 2]];
}
const near = (rgb, [r, g, b], tol = 30) =>
  Math.abs(rgb[0] - r) < tol && Math.abs(rgb[1] - g) < tol && Math.abs(rgb[2] - b) < tol;

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const ART = path.join(ROOT, 'artifacts');
export const DL = path.join(ROOT, 'downloads');

export function env() {
  const raw = fs.readFileSync(path.join(ROOT, '.env'), 'utf8');
  const e = {};
  for (const line of raw.split('\n')) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$/);
    if (m) e[m[1]] = m[2];
  }
  return e;
}

let step = 0;
export async function snap(page, name) {
  step++;
  const tag = String(step).padStart(2, '0') + '-' + name;
  await page.screenshot({ path: path.join(ART, tag + '.png') }).catch(() => {});
  console.log(`[snap] ${tag}`);
  return tag;
}

// Full login: Webswing HTML form + ERP canvas form. Leaves page on the ERP main screen.
export async function login(E, { headless = process.env.HEADLESS !== '0', onSessionGrid = null } = {}) {
  const browser = await chromium.launch({ headless });
  const ctx = await browser.newContext({
    acceptDownloads: true,
    viewport: { width: 1600, height: 900 },
    permissions: ['clipboard-read', 'clipboard-write'],
  });
  const page = await ctx.newPage();
  page.on('console', (m) => { if (m.type() === 'error') console.log('  console.error>', m.text().slice(0, 160)); });

  // goto tolerante: 'networkidle' não serve (Webswing mantém conexões abertas)
  let loaded = false;
  for (let i = 0; i < 3 && !loaded; i++) {
    try {
      await page.goto(E.ERP_URL, { waitUntil: 'domcontentloaded', timeout: 45000 });
      await page.waitForSelector('#nameInput', { timeout: 30000 });
      loaded = true;
    } catch (e) {
      console.log(`goto tentativa ${i + 1} falhou: ${String(e).slice(0, 100)}`);
      await page.waitForTimeout(4000);
    }
  }
  if (!loaded) return { browser, ctx, page, ok: false, state: 'no-load', reason: 'não carregou a tela de login do ERP' };

  await page.fill('#nameInput', E.ERP_USER1);
  await page.fill('#passwordInput', E.ERP_PASS1);
  await page.click('#loginFormSubmitButton');

  // Wait for ERP canvas login to render + "Efetuando rotinas de LOGIN..." to settle
  await page.waitForTimeout(45000);
  await snap(page, 'erp-login-screen');

  // TODO: às vezes aparece uma tela de SELEÇÃO DE BASE (vários blocos); a certa é sempre a
  // primeira. Mapear o clique quando essa tela for capturada em screenshot.

  // Canvas text entry: clica o campo, garante foco, limpa e digita devagar.
  // Webswing costuma perder o 1º caractere -> digita um espaço-guarda e remove depois.
  async function fill(x, y, text) {
    await page.mouse.click(x, y);
    await page.waitForTimeout(1200);
    await page.keyboard.press('Control+A');
    await page.waitForTimeout(200);
    await page.keyboard.press('Delete');
    await page.waitForTimeout(300);
    await page.keyboard.type(' ' + text, { delay: 110 });   // espaço-guarda
    await page.waitForTimeout(200);
    await page.keyboard.press('Home');
    await page.waitForTimeout(150);
    await page.keyboard.press('Delete');                     // remove o espaço-guarda
    await page.waitForTimeout(500);
  }

  const isBright = (p) => p[0] > 200 && p[1] > 200 && p[2] > 200;
  const isDark = (p) => p[0] < 60 && p[1] < 60 && p[2] < 70;
  const isBlue = (p) => p[2] > 110 && p[2] - p[0] > 40 && p[2] - p[1] > 20;

  // Preenche usuário/senha e envia; repete se continuar na tela de login (azul).
  let state = 'unknown';
  for (let attempt = 1; attempt <= 4 && state === 'unknown'; attempt++) {
    await fill(700, 538, E.ERP_USER2);   // Usuário
    await fill(888, 538, E.ERP_PASS2);   // Senha
    await snap(page, `erp-login-filled-${attempt}`);
    await page.mouse.click(975, 532);           // seta / enviar
    await page.waitForTimeout(3000);

    // Detecta resultado por pixel:
    //  bright(800,460) -> diálogo "excedeu sessões"
    //  dark   -> tela principal (login OK)
    //  blue   -> ainda na tela de login (paste falhou) -> tenta de novo
    let mainStreak = 0;
    for (let i = 0; i < 16; i++) {
      await page.waitForTimeout(5000);
      const a = await pixel(page, 800, 460);
      const b = await pixel(page, 260, 300);
      if (isBright(a)) {
        // "Sistema® - Novidades" (aviso de versão nova, ícone "i" azul em ~643,410):
        // não é erro — responde "Não" (961,561) e segue esperando a tela principal.
        const ic = await pixel(page, 630, 396);
        if (ic[2] > 180 && ic[0] < 80) {
          await snap(page, 'login-aviso-novidades');
          await page.mouse.click(961, 561);
          await page.waitForTimeout(2000);
          continue;
        }
        state = 'dialog'; break;
      }
      if (isDark(a) && isDark(b)) { mainStreak++; } else { mainStreak = 0; }
      if (mainStreak >= 2) { state = 'main'; break; }
      if (i >= 1 && isBlue(a) && isBlue(b)) break;   // travou no login -> re-tenta
    }
  }
  await snap(page, `login-result-${state}`);
  const reason = state === 'main' ? null
    : state === 'dialog'
      ? `Sessão anterior de "${E.ERP_USER2}" ainda ativa (limite 1). Aguarde o timeout.`
      : 'Falha ao preencher as credenciais no canvas.';
  // NUNCA fecha o browser aqui — o teardown() do script cuida disso (com logout best-effort),
  // inclusive nos casos de falha, para não deixar sessão pendurada.
  return { browser, ctx, page, ok: state === 'main', state, reason };
}

// Troca a empresa ativa. Botão ⇄ (canto sup. esq., ao lado de "Empresa:") abre a
// janela "Troca de empresa". Busca por sigla; se não resolver, tenta pela razão social.
// `alvo`: { sigla?, nome? }
export async function trocarEmpresa(page, alvo) {
  // A janela "Troca de empresa" tem uma faixa azul ("Troca da empresa em processamento")
  // em ~(700, 330). Enquanto a janela existe, esse pixel é azul forte.
  const dialogAberto = async () => {
    const p = await pixel(page, 700, 330);
    const azul = p[2] > 140 && p[2] - p[0] > 55 && p[2] - p[1] > 25;
    // tema atual do ERP (versão atual): faixa amarela (~192,165,41); fundo fechado é cinza (~31,34,37)
    const amarelo = p[0] > 140 && p[1] > 120 && p[0] - p[2] > 80;
    return azul || amarelo;
  };

  // botão ⇄ — às vezes a 1ª tentativa não abre a janela (instabilidade do Webswing): tenta até 5x
  let abriu = false;
  await page.waitForTimeout(4000);           // logo após o login o ERP ainda pode não aceitar cliques
  for (let t = 0; t < 5 && !abriu; t++) {
    if (t > 0) { for (let i = 0; i < 3; i++) { await page.keyboard.press('Escape').catch(() => {}); await page.waitForTimeout(300); } }
    await page.mouse.move(25, 30);
    await page.waitForTimeout(600);
    await page.mouse.click(25, 30);
    await page.waitForTimeout(4000 + t * 1500);
    abriu = await dialogAberto();
  }
  await snap(page, 'troca-empresa');
  if (!abriu) throw new Error('Janela "Troca de empresa" não abriu');

  async function tentar(campoY, valor, label) {
    await page.mouse.click(645, campoY);   // campo de texto (Sigla ~391 / Raz.Social ~413)
    await page.waitForTimeout(500);
    await page.keyboard.press('Control+A');
    await page.waitForTimeout(150);
    await page.evaluate((t) => navigator.clipboard.writeText(t), valor);
    await page.keyboard.press('Control+V');
    await page.waitForTimeout(1800);        // grade filtra p/ a linha correspondente
    await page.mouse.click(1023, 643);      // botão "OK" (NÃO usar Enter: vaza p/ a tela de fundo)
    await page.waitForTimeout(6000);
    const fechou = !(await dialogAberto());
    await snap(page, `troca-${label}`);
    if (fechou) {
      // empresa já ativa, diálogo informativo ou janela órfã (ex.: um tile em foco
      // que reagiu a um clique/tecla) — limpa a tela antes de seguir.
      for (let i = 0; i < 6; i++) { await page.keyboard.press('Escape').catch(() => {}); await page.waitForTimeout(300); }
      await page.waitForTimeout(1500);
      await snap(page, `troca-${label}-limpo`);
    }
    return fechou;                          // true = janela fechou = trocou
  }

  let ok = false;
  if (alvo.sigla) {
    await page.mouse.click(542, 392);      // radio "Sigla": o ERP lembra o último radio usado (se a última troca foi por Raz. Social, abre nela) — selecionar sempre explicitamente
    await page.waitForTimeout(400);
    ok = await tentar(391, alvo.sigla, 'sigla');
  }
  if (!ok && alvo.nome) {
    await page.mouse.click(542, 413);      // radio "Raz. Social"
    await page.waitForTimeout(400);
    ok = await tentar(413, alvo.nome, 'nome');
  }
  if (!ok) {
    await page.keyboard.press('Escape').catch(() => {});
    throw new Error(`Não consegui trocar para a empresa (sigla=${alvo.sigla || '-'}, nome=${alvo.nome || '-'})`);
  }
  await page.waitForTimeout(2000);
  await snap(page, 'empresa-trocada');
}

// --- detecção de estado "limpo" ---
// Duas condições encerram a sessão do ERP no servidor (o que importa p/ o limite):
//   a) tela de login do ERP (#nameInput)            -> tudo encerrado
//   b) "The application has been closed" (Webswing)   -> o app ERP saiu = sessão encerrada
async function atErpLogin(page) {
  return (await page.locator('#nameInput').count().catch(() => 0)) > 0;
}
async function appClosed(page) {
  return (await page.getByText('The application has been closed', { exact: false }).count().catch(() => 0)) > 0;
}
async function sessaoEncerrada(page) {
  return (await atErpLogin(page)) || (await appClosed(page));
}

// Logout com VERIFICAÇÃO. Retorna true só quando confirma que a sessão ERP encerrou.
// Em camadas e com retentativas — a sessão pendurada quebra o fluxo do time.
export async function logout(page) {
  try {
    for (let attempt = 1; attempt <= 5; attempt++) {
      if (await sessaoEncerrada(page)) break;

      // fecha qualquer janela/menu/modal aberto
      for (let i = 0; i < 6; i++) { await page.keyboard.press('Escape').catch(() => {}); await page.waitForTimeout(350); }
      // algumas janelas (ex.: "Visualização de recibos processados") NÃO fecham com Escape:
      // tenta o botão "Sair" (~1176,731), o OK de um aviso de redimensionamento (~1037,562)
      // e o X da janela maximizada (~1224,194).
      if (attempt >= 2) {
        await page.mouse.click(1176, 731).catch(() => {});
        await page.waitForTimeout(600);
        await page.mouse.click(1037, 562).catch(() => {});
        await page.waitForTimeout(600);
        await page.mouse.click(1224, 194).catch(() => {});
        await page.waitForTimeout(600);
        for (let i = 0; i < 4; i++) { await page.keyboard.press('Escape').catch(() => {}); await page.waitForTimeout(300); }
      }
      await page.waitForTimeout(700);

      // ERP logado: menu hamburguer -> "S-Sair" -> confirma (Sim)
      await page.mouse.click(25, 58).catch(() => {});
      await page.waitForTimeout(1400);
      await page.mouse.click(72, 117).catch(() => {});     // "S-Sair"
      await page.waitForTimeout(2500);
      await page.keyboard.press('Enter').catch(() => {});  // "Deseja realmente sair?" -> Sim
      await page.waitForTimeout(6000);
      await snap(page, `logout-try-${attempt}`);
    }

    // Webswing: sair da sessão de vez ("Sign out" / ícone do canvas)
    for (let i = 0; i < 4 && !(await atErpLogin(page)); i++) {
      await page.getByText('Sign out', { exact: false }).click({ timeout: 3000 }).catch(() => {});
      await page.waitForTimeout(2500);
      await page.mouse.click(1545, 878).catch(() => {});   // ícone logout no canto do canvas
      await page.waitForTimeout(1800);
    }
    await snap(page, 'logout-final');

    const encerrada = await sessaoEncerrada(page);
    console.log(encerrada
      ? `logout: OK — sessão ERP encerrada (${(await atErpLogin(page)) ? 'login do ERP' : 'app fechado'}).`
      : 'logout: FALHOU — NÃO confirmei o encerramento; a sessão pode segurar ~10min.');
    return encerrada;
  } catch (e) {
    console.log('logout() erro:', String(e).slice(0, 140));
    return false;
  }
}

// Encerra a automação SEMPRE tentando logout ANTES de fechar o navegador,
// e só fecha depois de confirmar (ou esgotar as tentativas).
export async function teardown({ browser, page }) {
  let ok = false;
  for (let round = 1; round <= 2 && !ok; round++) ok = await logout(page);
  if (!ok) console.log('teardown: ATENÇÃO — fechando o navegador SEM confirmar o logout do ERP.');
  await browser.close();
  return ok;
}
