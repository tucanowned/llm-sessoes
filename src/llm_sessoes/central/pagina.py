"""HTML do painel, gerado no servidor. Sem build de frontend: só um script inline para copiar.

Títulos, mensagens e caminhos vêm das sessões (conteúdo arbitrário): tudo passa por `_e`.
"""

from __future__ import annotations

import shlex
from html import escape
from pathlib import PurePosixPath

from .. import __version__, provedores

ATUALIZAR_A_CADA = 30

ESTILO = """
:root {
  --base: #1e1e2e; --mantle: #181825; --crust: #11111b;
  --surface0: #313244; --surface1: #45475a; --overlay0: #6c7086;
  --text: #cdd6f4; --subtext0: #a6adc8; --subtext1: #bac2de;
  --mauve: #cba6f7; --green: #a6e3a1; --red: #f38ba8; --peach: #fab387;
  --yellow: #f9e2af; --blue: #89b4fa; --teal: #94e2d5; --lavender: #b4befe;
  color-scheme: dark;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--crust); color: var(--text);
  font: 14px/1.45 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 20px 16px 48px; }
code, .mono { font-family: ui-monospace, "JetBrains Mono", "Cascadia Code", Menlo, monospace;
  font-size: 12.5px; }
header.topo { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px 20px;
  margin-bottom: 18px; }
header.topo h1 { margin: 0; font-size: 20px; color: var(--mauve); font-weight: 650; }
.numeros { display: flex; flex-wrap: wrap; gap: 6px 16px; color: var(--subtext0); }
.numeros b { color: var(--text); font-variant-numeric: tabular-nums; }
.numeros .alerta b { color: var(--red); }
.atualizado { margin-left: auto; color: var(--overlay0); font-size: 12px; }
section.maquina { background: var(--base); border: 1px solid var(--surface0);
  border-radius: 12px; padding: 14px 16px; margin-bottom: 16px; }
section.maquina.offline { opacity: .72; }
.cab { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 12px; }
.cab h2 { margin: 0; font-size: 16px; font-weight: 600; }
.cab .envio { color: var(--subtext0); font-size: 12.5px; }
.cab .versao { margin-left: auto; color: var(--overlay0); font-size: 12px; }
.pilula { display: inline-flex; align-items: center; gap: 5px; padding: 1px 9px;
  border-radius: 999px; font-size: 12px; font-weight: 600; }
.pilula::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.pilula.online { color: var(--green); background: rgba(166,227,161,.12); }
.pilula.offline { color: var(--overlay0); background: var(--surface0); }
.anomalias { margin: 12px 0 2px; padding: 9px 12px; border-radius: 8px;
  border: 1px solid rgba(250,179,135,.45); background: rgba(250,179,135,.08); color: var(--peach); }
.anomalias strong { display: block; margin-bottom: 2px; }
.anomalias ul { margin: 0; padding-left: 18px; }
.anomalias li { color: var(--yellow); }
h3 { margin: 16px 0 6px; font-size: 12px; font-weight: 600; letter-spacing: .06em;
  text-transform: uppercase; color: var(--subtext0); }
h3 .n { color: var(--overlay0); }
ul.sessoes { list-style: none; margin: 0; padding: 0; }
li.sessao { display: grid; grid-template-columns: 1fr auto; gap: 2px 12px; padding: 9px 10px;
  border-radius: 8px; border-left: 3px solid transparent; }
li.sessao + li.sessao { border-top: 1px solid var(--surface0); }
li.sessao:hover { background: var(--mantle); }
li.sessao.trabalhando { border-left-color: var(--green); }
li.sessao.crash { border-left-color: var(--red); }
.l1 { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
.titulo { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sem-titulo { color: var(--overlay0); font-style: italic; font-weight: 400; }
.selo { flex: none; font-size: 11px; font-weight: 700; padding: 0 6px; border-radius: 4px;
  background: var(--surface0); color: var(--subtext1); }
.selo.claude { color: var(--peach); }
.selo.codex { color: var(--blue); }
.l2 { grid-column: 1; display: flex; flex-wrap: wrap; gap: 2px 14px; color: var(--subtext0);
  font-size: 12.5px; }
.l2 .proj { color: var(--text); }
.l2 .wt { color: var(--lavender); }
.l2 .tmux { color: var(--teal); }
.estado.trabalhando { color: var(--green); }
.estado.parada { color: var(--yellow); }
.estado.crash { color: var(--red); font-weight: 600; }
.estado.fechou { color: var(--subtext0); }
.msg { grid-column: 1; color: var(--overlay0); font-size: 12.5px; font-style: italic;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
button.copiar { grid-column: 2; grid-row: 1 / span 3; align-self: center; cursor: pointer;
  font: 600 12px/1 inherit; font-family: inherit; color: var(--mauve);
  background: rgba(203,166,247,.1); border: 1px solid rgba(203,166,247,.35); border-radius: 7px;
  padding: 7px 11px; white-space: nowrap; }
button.copiar:hover { background: rgba(203,166,247,.2); }
button.copiar:focus-visible { outline: 2px solid var(--mauve); outline-offset: 2px; }
button.copiar.ok { color: var(--green); border-color: rgba(166,227,161,.5); }
.vazio { color: var(--overlay0); font-size: 13px; padding: 4px 10px; }
.nada { text-align: center; color: var(--subtext0); padding: 60px 16px; }
@media (max-width: 600px) {
  li.sessao { grid-template-columns: 1fr; }
  button.copiar { grid-column: 1; grid-row: auto; justify-self: start; margin-top: 6px; }
  .cab .versao, .atualizado { margin-left: 0; }
}
"""

SCRIPT = """
document.addEventListener('click', async (ev) => {
  const b = ev.target.closest('button.copiar');
  if (!b) return;
  const cmd = b.dataset.cmd;
  let ok = false;
  try { await navigator.clipboard.writeText(cmd); ok = true; } catch (e) {
    const t = document.createElement('textarea');
    t.value = cmd; t.style.position = 'fixed'; t.style.opacity = '0';
    document.body.appendChild(t); t.select();
    try { ok = document.execCommand('copy'); } catch (e2) {}
    t.remove();
  }
  if (!ok) { window.prompt('Copie o comando:', cmd); return; }
  const antes = b.textContent;
  b.textContent = 'copiado ✓'; b.classList.add('ok');
  setTimeout(() => { b.textContent = antes; b.classList.remove('ok'); }, 1600);
});
for (const el of document.querySelectorAll('[data-epoch]')) {
  el.title = new Date(Number(el.dataset.epoch) * 1000).toLocaleString();
}
"""


def renderizar(maquinas: list[dict], resumo: dict, agora: int, nonce: str) -> str:
    blocos = "\n".join(_maquina(m, agora) for m in maquinas) or (
        '<p class="nada">Nenhuma máquina enviou relatório ainda. '
        'Configure o cliente com <code>LLM_SESSOES_CENTRAL_URL</code> e rode '
        '<code>llm-sessoes enviar</code>.</p>')
    crashes = resumo["crashes_24h"]
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{ATUALIZAR_A_CADA}">
<title>Sessões LLM</title>
<style nonce="{nonce}">{ESTILO}</style>
</head>
<body>
<main>
<header class="topo">
  <h1>Sessões LLM</h1>
  <div class="numeros">
    <span><b>{resumo["ativas"]}</b> ativa{"s" if resumo["ativas"] != 1 else ""}</span>
    <span><b>{resumo["maquinas_online"]}/{resumo["maquinas_total"]}</b> máquinas online</span>
    <span class="{"alerta" if crashes else ""}"><b>{crashes}</b> crash{"es" if crashes != 1 else ""} em 24 h</span>
  </div>
  <span class="atualizado" data-epoch="{agora}">atualiza a cada {ATUALIZAR_A_CADA} s · v{_e(__version__)}</span>
</header>
{blocos}
</main>
<script nonce="{nonce}">{SCRIPT}</script>
</body>
</html>
"""


def comando_retomar(maquina: str, sessao: dict) -> str | None:
    """O que o botão copia: anexar ao tmux onde a sessão está viva, ou retomá-la remotamente."""
    tmux = sessao.get("tmux")
    if tmux and tmux.get("sessao"):
        return f"ssh {shlex.quote(maquina)} -t tmux attach -t {shlex.quote(tmux['sessao'])}"
    try:
        resume = provedores.por_nome(sessao["provedor"]).comando_resume(sessao["session_id"])
    except KeyError:
        return None  # provedor que este servidor não conhece
    if not sessao.get("dir_resume"):
        return None
    interno = f"cd {shlex.quote(sessao['dir_resume'])} && {shlex.join(resume)}"
    return f"ssh {shlex.quote(maquina)} -t {shlex.quote(interno)}"


def ha_quanto(quando: int | None, agora: int) -> str:
    if quando is None:
        return "?"
    s = max(0, agora - quando)
    if s < 60:
        return f"há {s} s"
    if s < 3600:
        return f"há {s // 60} min"
    if s < 48 * 3600:
        return f"há {s // 3600} h"
    return f"há {s // 86400} d"


# --- partes ------------------------------------------------------------------------------


def _maquina(m: dict, agora: int) -> str:
    estado = "online" if m["online"] else "offline"
    anomalias = ""
    if m["anomalias"]:
        itens = "".join(f"<li>{_e(a)}</li>" for a in m["anomalias"])
        anomalias = (f'<div class="anomalias" role="alert"><strong>⚠ Anomalias na última '
                     f'coleta — fechamentos desse provedor não estão sendo registrados</strong>'
                     f'<ul>{itens}</ul></div>')
    vivas = "".join(_sessao(m["nome"], s, agora, viva=True) for s in m["vivas"])
    recentes = "".join(_sessao(m["nome"], s, agora, viva=False) for s in m["recentes"])
    rotulo_vivas = "Ativas" if m["online"] else "Ativas no último envio"
    return f"""<section class="maquina {estado}">
  <div class="cab">
    <h2>{_e(m["nome"])}</h2>
    <span class="pilula {estado}">{estado}</span>
    <span class="envio" data-epoch="{m["recebido_em"]}">último envio {ha_quanto(m["recebido_em"], agora)}</span>
    <span class="versao">{_e(f'cliente v{m["versao_cliente"]}' if m["versao_cliente"] else "")}</span>
  </div>
  {anomalias}
  <h3>{rotulo_vivas} <span class="n">{len(m["vivas"])}</span></h3>
  {f'<ul class="sessoes">{vivas}</ul>' if vivas else '<p class="vazio">Nenhuma sessão aberta.</p>'}
  <h3>Fechadas e crash · 48 h <span class="n">{len(m["recentes"])}</span></h3>
  {f'<ul class="sessoes">{recentes}</ul>' if recentes else '<p class="vazio">Nada nas últimas 48 h.</p>'}
</section>"""


def _sessao(maquina: str, s: dict, agora: int, viva: bool) -> str:
    titulo = (f'<span class="titulo">{_e(s["titulo"])}</span>' if s.get("titulo")
              else '<span class="titulo sem-titulo">(sem título)</span>')
    partes = [_projeto(s)]
    if s.get("branch"):
        partes.append(f'<span title="branch">⎇ {_e(s["branch"])}</span>')

    if viva:
        status = s.get("status")
        quando = s.get("ultima_atividade") or s.get("inicio")
        if status in ("trabalhando", "parada"):
            texto, classe = f"● {status} {ha_quanto(quando, agora)}", status
        else:
            texto, classe = f"atividade {ha_quanto(quando, agora)}", ""
        partes.append(f'<span class="estado {classe}" data-epoch="{quando or ""}">{_e(texto)}</span>')
        if s.get("inicio"):
            partes.append(f'<span data-epoch="{s["inicio"]}">aberta {ha_quanto(s["inicio"], agora)}</span>')
        tmux = s.get("tmux")
        if tmux:
            janela = f':{tmux["janela"]}' if tmux.get("janela") is not None else ""
            partes.append(f'<span class="tmux">tmux <code>{_e(tmux["sessao"] + janela)}</code></span>')
        classe_li = "trabalhando" if status == "trabalhando" else ""
    else:
        tipo = s["tipo"]
        texto = "✖ crash" if tipo == "crash" else "fechada"
        partes.append(f'<span class="estado {tipo}" data-epoch="{s["quando"]}">'
                      f'{texto} {ha_quanto(s["quando"], agora)}</span>')
        classe_li = "crash" if tipo == "crash" else ""

    msg = f'<div class="msg">“{_e(s["ultima_msg"])}”</div>' if s.get("ultima_msg") else ""
    comando = comando_retomar(maquina, s)
    botao = ""
    if comando:
        rotulo = "copiar tmux attach" if viva and s.get("tmux") else "copiar resume"
        botao = (f'<button class="copiar" type="button" data-cmd="{_e(comando)}" '
                 f'title="{_e(comando)}">{rotulo}</button>')
    provedor = _e(s["provedor"])
    return f"""
    <li class="sessao {classe_li}">
      <div class="l1"><span class="selo {provedor}">{provedor}</span>{titulo}</div>
      <div class="l2">{"".join(partes)}</div>
      {msg}{botao}
    </li>"""


def _projeto(s: dict) -> str:
    resume = s.get("dir_resume") or ""
    trabalho = s.get("dir_trabalho") or resume
    nome = PurePosixPath(resume).name or resume or "?"
    html = f'<span class="proj mono" title="{_e(resume)}">{_e(nome)}</span>'
    if trabalho and trabalho != resume:
        html += (f'<span class="wt mono" title="{_e(trabalho)}">⑂ '
                 f'{_e(PurePosixPath(trabalho).name)}</span>')
    return html


def _e(texto) -> str:
    return escape(str(texto), quote=True)
