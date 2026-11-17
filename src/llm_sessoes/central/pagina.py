"""HTML do painel, gerado no servidor. Sem build de frontend: só um script inline para copiar.

Títulos, mensagens e caminhos vêm das sessões (conteúdo arbitrário): tudo passa por `_e`.
"""

from __future__ import annotations

import re
import shlex
from html import escape
from pathlib import PurePosixPath

from .. import __version__, provedores

ATUALIZAR_A_CADA = 30
ABAS = {"ativas": "Ativas", "recentes": "Fechadas e crash"}
ABA_PADRAO = "ativas"

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
nav.abas { display: flex; gap: 4px; margin: 0 0 16px; border-bottom: 1px solid var(--surface0); }
nav.abas a { display: inline-flex; align-items: center; gap: 7px; padding: 8px 14px;
  color: var(--subtext0); text-decoration: none; font-weight: 600; border-bottom: 2px solid transparent;
  margin-bottom: -1px; border-radius: 6px 6px 0 0; }
nav.abas a:hover { color: var(--text); background: var(--base); }
nav.abas a:focus-visible { outline: 2px solid var(--mauve); outline-offset: -2px; }
nav.abas a[aria-current="page"] { color: var(--mauve); border-bottom-color: var(--mauve); }
nav.abas .n { font-size: 12px; font-variant-numeric: tabular-nums; padding: 0 7px;
  border-radius: 999px; background: var(--surface0); color: var(--subtext1); }
nav.abas a.alerta .n { background: rgba(243,139,168,.18); color: var(--red); }
nav.abas .crash24 { font-size: 11.5px; font-weight: 600; color: var(--red); }
section.maquina.compacta { padding: 9px 16px; }
.cab .nada-aqui { color: var(--overlay0); font-size: 13px; }
.cab .sep { color: var(--overlay0); }
.nota-offline { margin: 8px 0 0; color: var(--overlay0); font-size: 12.5px; }
.evento { flex: none; font-size: 11px; font-weight: 700; padding: 0 6px; border-radius: 4px; }
.evento.crash { color: var(--red); background: rgba(243,139,168,.15); }
.evento.fechou { color: var(--subtext0); background: var(--surface0); }
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
ul.sessoes { list-style: none; margin: 10px 0 0; padding: 0; }
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


def aba_valida(valor: str | None) -> str:
    """Valor desconhecido (ou ausente) em `?aba=` cai no padrão, sem erro."""
    return valor if valor in ABAS else ABA_PADRAO


def renderizar(maquinas: list[dict], resumo: dict, agora: int, nonce: str,
               aba: str | None = ABA_PADRAO) -> str:
    aba = aba_valida(aba)  # daqui em diante só um dos valores conhecidos é reaproveitado
    blocos = "\n".join(_maquina(m, agora, aba) for m in maquinas) or (
        '<p class="nada">Nenhuma máquina enviou relatório ainda. '
        'Configure o cliente com <code>LLM_SESSOES_CENTRAL_URL</code> e rode '
        '<code>llm-sessoes enviar</code>.</p>')
    crashes = resumo["crashes_24h"]
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{ATUALIZAR_A_CADA}; url={_e(_url_aba(aba))}">
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
{_abas(maquinas, aba, crashes)}
{blocos}
</main>
<script nonce="{nonce}">{SCRIPT}</script>
</body>
</html>
"""


def comando_retomar(maquina: str, sessao: dict) -> str | None:
    """O que o botão copia: anexar ao tmux onde a sessão está viva, ou retomá-la num tmux novo.

    O resume roda por um shell interativo (`$SHELL -ic`) porque, por ssh sem tty interativo, o
    `claude` não está no PATH e os segredos do ~/.zshrc não são carregados. Dentro do tmux a
    sessão sobrevive à queda do ssh, e o `-A` faz um segundo clique reconectar em vez de abrir
    outra.
    """
    tmux = sessao.get("tmux")
    if tmux and tmux.get("sessao"):
        return _ssh(maquina, ["tmux", "attach", "-t", tmux["sessao"]])
    try:
        resume = provedores.por_nome(sessao["provedor"]).comando_resume(sessao["session_id"])
    except KeyError:
        return None  # provedor que este servidor não conhece
    if not sessao.get("dir_resume"):
        return None
    # O tmux roda esta string com o shell padrão dele; o $SHELL fica sem aspas para expandir lá.
    interativo = f"${{SHELL:-zsh}} -ic {shlex.quote(shlex.join(resume))}"
    return _ssh(maquina, ["tmux", "new", "-A", "-s", nome_tmux(sessao), "-c",
                          sessao["dir_resume"], interativo])


def nome_tmux(sessao: dict) -> str:
    """`claude-a78c1f02`: provedor + começo do id, só com o que o tmux aceita em nome de sessão."""
    bruto = f"{sessao['provedor']}-{sessao['session_id'][:8]}"
    return re.sub(r"[^A-Za-z0-9_-]", "_", bruto)


def _ssh(maquina: str, remoto: list[str]) -> str:
    """`ssh <maquina> -t <argv remoto>` pronto para colar num shell.

    São duas camadas de shell: a local separa os argumentos do ssh, e o ssh os junta com espaço
    numa linha que o shell remoto separa de novo. Por isso cada argumento remoto leva aspas
    duas vezes (quando não precisa de aspas, fica igual: `tmux new -A …` continua legível).
    """
    return shlex.join(["ssh", maquina, "-t"]) + " " + " ".join(
        shlex.quote(shlex.quote(arg)) for arg in remoto)


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


def _abas(maquinas: list[dict], atual: str, crashes_24h: int) -> str:
    """Links comuns gerados no servidor: nada de JavaScript (a CSP só libera o script do copiar)."""
    contagem = {"ativas": sum(len(m["vivas"]) for m in maquinas),
                "recentes": sum(len(m["recentes"]) for m in maquinas)}
    links = []
    for aba, rotulo in ABAS.items():
        atributos = ' aria-current="page"' if aba == atual else ""
        alerta = ""
        if aba == "recentes" and crashes_24h:
            atributos += ' class="alerta"'
            alerta = (f' <span class="crash24">● {crashes_24h} crash'
                      f'{"es" if crashes_24h != 1 else ""} em 24 h</span>')
        links.append(f'<a href="{_e(_url_aba(aba))}"{atributos}>{_e(rotulo)} '
                     f'<span class="n">{contagem[aba]}</span>{alerta}</a>')
    return f'<nav class="abas" aria-label="Abas">{"".join(links)}</nav>'


def _url_aba(aba: str) -> str:
    # Relativa: mantém o caminho atual, e o painel continua funcionando atrás de um prefixo.
    return f"?aba={aba}"


def _maquina(m: dict, agora: int, aba: str) -> str:
    estado = "online" if m["online"] else "offline"
    anomalias = ""
    if m["anomalias"]:
        itens = "".join(f"<li>{_e(a)}</li>" for a in m["anomalias"])
        anomalias = (f'<div class="anomalias" role="alert"><strong>⚠ Anomalias na última '
                     f'coleta — fechamentos desse provedor não estão sendo registrados</strong>'
                     f'<ul>{itens}</ul></div>')
    viva = aba == "ativas"
    sessoes = m["vivas"] if viva else m["recentes"]  # recentes já vêm da mais nova à mais antiga
    cab = f"""<h2>{_e(m["nome"])}</h2>
    <span class="pilula {estado}">{estado}</span>
    <span class="envio" data-epoch="{m["recebido_em"]}">último envio {ha_quanto(m["recebido_em"], agora)}</span>"""

    if not sessoes:
        # Máquina sem nada nesta aba: uma linha, não um bloco vazio.
        vazio = "nenhuma sessão ativa" if viva else "nada fechado nas últimas 48 h"
        return f"""<section class="maquina compacta {estado}">
  <div class="cab">
    {cab.replace("</h2>", f'</h2><span class="sep">·</span><span class="nada-aqui">{vazio}</span>', 1)}
  </div>
  {anomalias}
</section>"""

    itens = "".join(_sessao(m["nome"], s, agora, viva=viva) for s in sessoes)
    nota = ('<p class="nota-offline">Máquina offline: sessões segundo o último envio.</p>'
            if viva and not m["online"] else "")
    return f"""<section class="maquina {estado}">
  <div class="cab">
    {cab}
    <span class="versao">{_e(f'cliente v{m["versao_cliente"]}' if m["versao_cliente"] else "")}</span>
  </div>
  {anomalias}{nota}
  <ul class="sessoes">{itens}</ul>
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
        partes.append(f'<span class="estado {tipo}" data-epoch="{s["quando"]}">'
                      f'{ha_quanto(s["quando"], agora)}</span>')
        classe_li = "crash" if tipo == "crash" else ""

    msg = f'<div class="msg">“{_e(s["ultima_msg"])}”</div>' if s.get("ultima_msg") else ""
    comando = comando_retomar(maquina, s)
    botao = ""
    if comando:
        rotulo = "copiar tmux attach" if viva and s.get("tmux") else "copiar resume"
        botao = (f'<button class="copiar" type="button" data-cmd="{_e(comando)}" '
                 f'title="{_e(comando)}">{rotulo}</button>')
    provedor = _e(s["provedor"])
    evento = ""
    if not viva:
        evento = f'<span class="evento {_e(s["tipo"])}">{_e(s["tipo"])}</span>'
    return f"""
    <li class="sessao {classe_li}">
      <div class="l1">{evento}<span class="selo {provedor}">{provedor}</span>{titulo}</div>
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
