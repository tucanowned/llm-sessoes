from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import provedores, reabrir, sistema
from .banco import Banco, caminho_padrao
from .coletor import coletar, registrar_fechamento

MARCA_CRON = "# llm-sessoes"
INTERVALO_CRON_MIN = 2
AVISO_CRASH_DIAS = 7
"""Crash mais velho que isso não é mais lembrado a cada boot."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="llm-sessoes", description=__doc__)
    sub = parser.add_subparsers(dest="comando", required=True, metavar="comando")

    p = sub.add_parser("coletar", help="faz uma coleta (é o que o cron chama)")
    p.add_argument("-v", "--verbose", action="store_true", help="imprime mesmo sem mudanças")

    sub.add_parser("status", help="sessões vivas segundo a última coleta")

    p = sub.add_parser("fechadas", help="sessões fechadas recentemente e não reabertas")
    p.add_argument("--horas", type=float, default=24)

    sub.add_parser("crash", help="sessões que estavam abertas no último crash")

    p = sub.add_parser("reabrir", help="reabre sessões em abas do Windows Terminal")
    p.add_argument("prefixos", nargs="*", metavar="prefixo-do-id")
    p.add_argument("--crash", action="store_true", help="as pendentes do último crash")
    p.add_argument("--fechadas", action="store_true", help="as fechadas e não reabertas")
    p.add_argument("--horas", type=float, default=24, help="janela do --fechadas (padrão 24)")
    p.add_argument("-n", "--simular", action="store_true", help="só mostra o que abriria")

    # Interno: é o que roda dentro da aba aberta pelo `reabrir`.
    p = sub.add_parser("_aba")
    p.add_argument("provedor")
    p.add_argument("session_id")

    sub.add_parser("aviso", help="avisa, uma vez por boot, se há sessões de um crash a reabrir "
                                 "(para o .zshrc)")

    p = sub.add_parser("hook", help="recebe no stdin o JSON de um hook da ferramenta "
                                    "(SessionEnd do Claude Code)")
    p.add_argument("provedor", choices=["claude"])

    sub.add_parser("tui", help="interface para buscar, marcar e reabrir sessões (requer Textual)")

    p = sub.add_parser("cron", help="agenda/remove a coleta periódica")
    p.add_argument("acao", choices=["instalar", "remover", "ver"])

    args = parser.parse_args(argv)
    if args.comando == "cron":
        return _cron(args.acao)
    if args.comando == "tui":
        return _tui()

    if args.comando in ("aviso", "hook"):
        # Rodam dentro do shell e do Claude Code: nunca podem atrapalhar quem os chamou.
        try:
            banco = Banco(caminho_padrao())
            try:
                return {"aviso": _aviso, "hook": _hook}[args.comando](banco, args)
            finally:
                banco.fechar()
        except Exception:
            return 0

    banco = Banco(caminho_padrao())
    try:
        return {"coletar": _coletar, "status": _status,
                "fechadas": _fechadas, "crash": _crash,
                "reabrir": _reabrir, "_aba": _aba}[args.comando](banco, args)
    finally:
        banco.fechar()


# --- comandos -------------------------------------------------------------------


def _coletar(banco: Banco, args) -> int:
    r = coletar(banco, provedores.todos(), int(time.time()), sistema.boot_id())
    if args.verbose or r.abriram or r.fecharam or r.crash or r.avisos or r.notas:
        print(f"{_hora(time.time())} vivas={r.vivas} abriram={len(r.abriram)} "
              f"fecharam={len(r.fecharam)} crash={len(r.crash)}")
    for nota in r.notas:
        print(f"  nota: {nota}")
    for aviso in r.avisos:
        print(f"  AVISO: {aviso}", file=sys.stderr)
    return 1 if r.avisos else 0


def _status(banco: Banco, args) -> int:
    ultima = banco.meta("ultima_coleta")
    if not ultima:
        print("Nenhuma coleta feita ainda. Rode: llm-sessoes coletar")
        return 1
    vivas = banco.vivas()
    for aviso in json.loads(banco.meta("avisos") or "[]"):
        print(f"⚠ {aviso}")
        print("  (enquanto isso durar, fechamentos desse provedor não são registrados)\n")
    print(f"{len(vivas)} sessão(ões) viva(s) — última coleta {_hora(int(ultima))}\n")
    for s in vivas:
        _imprimir(s, f"{s['tty'] or '?':7} {s['status'] or '':5} desde {_hora(s['desde'])}")
    return 0


def _fechadas(banco: Banco, args) -> int:
    fechadas = banco.fechadas(desde=int(time.time() - args.horas * 3600))
    if not fechadas:
        print(f"Nenhuma sessão fechada (e não reaberta) nas últimas {args.horas:g}h.")
        return 0
    for s in fechadas:
        _imprimir(s, f"fechada ~{_hora(s['quando'])} ({s['tty'] or '?'})")
    return 0


def _crash(banco: Banco, args) -> int:
    sessoes = banco.ultimo_crash()
    if not sessoes:
        print("Nenhum crash registrado.")
        return 0
    pendentes = [s for s in sessoes if not s["reaberta"]]
    print(f"Último crash: {len(sessoes)} sessão(ões) abertas às {_hora(sessoes[0]['quando'])}, "
          f"{len(pendentes)} ainda não reaberta(s)\n")
    for s in sessoes:
        _imprimir(s, "já reaberta" if s["reaberta"] else "PENDENTE")
    return 0


def _reabrir(banco: Banco, args) -> int:
    if not (args.crash or args.fechadas or args.prefixos):
        print("Diga o que reabrir: --crash, --fechadas e/ou prefixos de id.", file=sys.stderr)
        return 2

    sessoes = {}
    if args.crash:
        sessoes.update((_chave(s), s) for s in banco.ultimo_crash() if not s["reaberta"])
    if args.fechadas:
        desde = int(time.time() - args.horas * 3600)
        sessoes.update((_chave(s), s) for s in banco.fechadas(desde))
    for prefixo in args.prefixos:
        achadas = banco.por_prefixo(prefixo)
        if len(achadas) != 1:
            print(f"Prefixo '{prefixo}': " + ("nenhuma sessão." if not achadas else "ambíguo:"),
                  file=sys.stderr)
            for s in achadas:
                print(f"    {s['session_id']}  [{s['provedor']}] {s['titulo'] or '(sem título)'}",
                      file=sys.stderr)
            return 2
        if achadas[0]["viva"]:
            print(f"Já está aberta, pulando: {_rotulo(achadas[0])}")
            continue
        sessoes.setdefault(_chave(achadas[0]), achadas[0])

    if not sessoes:
        print("Nada para reabrir.")
        return 0
    try:
        r = reabrir.reabrir(sessoes.values(), simular=args.simular)
    except reabrir.ErroReabrir as e:
        print(f"ERRO: {e}", file=sys.stderr)
        return 1
    for s in r.abertas:
        print(f"{'abriria' if args.simular else 'aberta '}  {_rotulo(s)}  →  {_til(s['dir_resume'])}")
    for s, motivo in r.puladas:
        print(f"PULADA   {_rotulo(s)} — {motivo}")
    return 1 if r.puladas else 0


def _aba(banco: Banco, args) -> int:
    achadas = [s for s in banco.por_prefixo(args.session_id)
               if s["provedor"] == args.provedor and s["session_id"] == args.session_id]
    if not achadas:
        print(f"Sessão desconhecida: {args.provedor} {args.session_id}", file=sys.stderr)
        return 1
    sessao = dict(achadas[0])
    banco.fechar()
    reabrir.rodar_na_aba(sessao)  # não retorna: termina num exec do shell
    return 0


def _aviso(banco: Banco, args) -> int:
    boot = sistema.boot_id()
    if banco.meta("aviso_boot") == boot:
        return 0
    if banco.meta("boot_id") != boot:
        # Primeiro shell antes da primeira rodada do cron: é esta coleta que registra o crash.
        coletar(banco, provedores.todos(), int(time.time()), boot)
    with banco.con:
        banco.definir_meta("aviso_boot", boot)
    recente = time.time() - AVISO_CRASH_DIAS * 86400
    pendentes = [s for s in banco.ultimo_crash() if not s["reaberta"] and s["quando"] >= recente]
    if not pendentes:
        return 0
    n = len(pendentes)
    print(f"▲ llm-sessoes: {n} sess{'ões estavam abertas' if n != 1 else 'ão estava aberta'} "
          f"quando a máquina caiu ({_hora(pendentes[0]['quando'])}).")
    for s in pendentes[:5]:
        print(f"    [{s['provedor']}] {s['titulo'] or '(sem título)'}")
    if n > 5:
        print(f"    … e mais {n - 5}")
    print("  Escolher o que reabrir: llm-sessoes tui    Reabrir tudo: llm-sessoes reabrir --crash")
    return 0


def _hook(banco: Banco, args) -> int:
    evento = json.load(sys.stdin)
    if evento.get("hook_event_name") == "SessionEnd" and evento.get("session_id"):
        registrar_fechamento(banco, args.provedor, evento["session_id"], int(time.time()),
                             sistema.boot_id())
    return 0


def _tui() -> int:
    # O Windows Terminal é truecolor mas não anuncia; sem isso o Textual cai para 256 cores e o
    # fundo do tema vira preto puro.
    if os.environ.get("WT_SESSION") and not os.environ.get("COLORTERM"):
        os.environ["COLORTERM"] = "truecolor"
    try:
        from .tui.app import SessoesApp
    except ModuleNotFoundError:
        # O lançador usa o python do sistema (o coletor não pode depender de venv); o Textual
        # mora no .venv do repositório, então a TUI se reexecuta com ele.
        raiz = Path(__file__).resolve().parents[2]
        python = raiz / ".venv" / "bin" / "python"
        if python.exists() and Path(sys.prefix).resolve() != (raiz / ".venv").resolve():
            os.execv(python, [str(python), str(raiz / "bin" / "llm-sessoes"), "tui"])
        print("A TUI precisa do Textual. No repositório:\n"
              "  python3 -m venv .venv && .venv/bin/pip install -r requirements-tui.txt\n"
              "(sem python3-venv: python3 -m venv --without-pip .venv && "
              "pip3 --python .venv/bin/python install -r requirements-tui.txt)", file=sys.stderr)
        return 1
    SessoesApp(caminho_padrao()).run()
    return 0


def _cron(acao: str) -> int:
    atual = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    linhas = atual.stdout.splitlines() if atual.returncode == 0 else []
    if acao == "ver":
        print("\n".join(l for l in linhas if MARCA_CRON in l) or "(não instalado)")
        return 0

    linhas = [l for l in linhas if MARCA_CRON not in l]
    if acao == "instalar":
        executavel = Path(__file__).resolve().parents[2] / "bin" / "llm-sessoes"
        log = Path.home() / ".local/state/llm-sessoes/coletor.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        linhas.append(f"*/{INTERVALO_CRON_MIN} * * * * {shlex.quote(str(executavel))} coletar "
                      f">> {shlex.quote(str(log))} 2>&1 {MARCA_CRON}")
    subprocess.run(["crontab", "-"], input="\n".join(linhas) + "\n", text=True, check=True)
    print(f"cron {'instalado' if acao == 'instalar' else 'removido'}.")
    return 0


# --- apresentação -----------------------------------------------------------------


def _imprimir(s, situacao: str) -> None:
    print(f"[{s['provedor']}] {s['titulo'] or '(sem título)'}  —  {situacao}")
    trabalho = s["dir_trabalho"]
    if trabalho and trabalho != s["dir_resume"]:
        print(f"    trabalhando em: {_til(trabalho)}")
    if s["branch"]:
        print(f"    branch: {s['branch']}")
    if s["ultima_msg"]:
        print(f"    últ. msg: {s['ultima_msg'][:110]}")
    comando = provedores.por_nome(s["provedor"]).comando_resume(s["session_id"])
    print(f"    cd {shlex.quote(s['dir_resume'])} && {shlex.join(comando)}\n")


def _chave(s) -> tuple[str, str]:
    return (s["provedor"], s["session_id"])


def _rotulo(s) -> str:
    return f"[{s['provedor']}] {s['titulo'] or '(sem título)'} ({s['session_id'][:8]})"


def _hora(epoch: float) -> str:
    quando = datetime.fromtimestamp(epoch)
    formato = "%H:%M" if quando.date() == datetime.now().date() else "%d/%m %H:%M"
    return quando.strftime(formato)


def _til(caminho: str) -> str:
    casa = str(Path.home())
    return "~" + caminho[len(casa):] if caminho.startswith(casa) else caminho
