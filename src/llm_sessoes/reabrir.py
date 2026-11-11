"""Reabre sessões em abas do Windows Terminal (WSL).

Dividido em três partes: a montagem dos argumentos do `wt.exe` (lógica pura), a execução
(subprocess) e o modo interno `_aba`, que é o que roda dentro da aba recém-aberta.

O comando passado ao `wt.exe` leva só provedor e id da sessão — o diretório é lido do banco já
dentro da aba. Assim nada que venha do usuário (caminho com espaço, `;`, aspas) atravessa o parsing
de linha de comando do wt e do wsl.exe; o wt, em especial, trata `;` como separador de comandos.
"""

from __future__ import annotations

import glob
import os
import pwd
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from . import provedores

WSL_EXE = r"C:\Windows\System32\wsl.exe"
GLOB_WT = "/mnt/c/Users/*/AppData/Local/Microsoft/WindowsApps/wt.exe"
DISTRO_PADRAO = "Debian"
LIMITE_TITULO = 60
PAUSA_ENTRE_ABAS = 0.5
"""Segundos entre um `new-tab` e outro — sem pausa o wt às vezes embaralha ou perde abas."""


class ErroReabrir(Exception):
    pass


@dataclass(frozen=True)
class Resultado:
    abertas: list[Mapping]
    puladas: list[tuple[Mapping, str]]
    """(sessão, motivo)"""


# --- lógica pura ----------------------------------------------------------------


def titulo_da_aba(sessao: Mapping) -> str:
    titulo = sessao["titulo"] or Path(sessao["dir_resume"]).name or sessao["session_id"][:8]
    # `;` separa comandos no wt e aspas atrapalham a conversão para linha de comando do Windows.
    titulo = titulo.replace(";", ",").replace('"', "'")
    titulo = " ".join(titulo.split())
    if len(titulo) > LIMITE_TITULO:
        titulo = titulo[:LIMITE_TITULO - 1] + "…"
    return f"{sessao['provedor']}: {titulo}"


def argumentos_wt(wt: str, distro: str, lancador: str, sessao: Mapping) -> list[str]:
    """Linha de comando que abre uma aba para a sessão, na janela mais recente do wt."""
    return [
        wt, "-w", "0", "new-tab", "--title", titulo_da_aba(sessao), "--suppressApplicationTitle",
        WSL_EXE, "-d", distro, "--",
        lancador, "_aba", sessao["provedor"], sessao["session_id"],
    ]


def motivo_para_pular(sessao: Mapping, existe_dir: Callable[[str], bool] = os.path.isdir) -> str | None:
    if not existe_dir(sessao["dir_resume"]):
        return f"diretório não existe mais: {sessao['dir_resume']}"
    return None


# --- ambiente ---------------------------------------------------------------------


def localizar_wt() -> str:
    candidatos = [os.environ.get("LLM_SESSOES_WT"), shutil.which("wt.exe"), *sorted(glob.glob(GLOB_WT))]
    for candidato in candidatos:
        if candidato and os.path.exists(candidato):
            return candidato
    raise ErroReabrir("wt.exe não encontrado — aponte com a variável LLM_SESSOES_WT")


def distro() -> str:
    return os.environ.get("WSL_DISTRO_NAME") or DISTRO_PADRAO


def lancador() -> str:
    return str(Path(__file__).resolve().parents[2] / "bin" / "llm-sessoes")


# --- execução ---------------------------------------------------------------------


def reabrir(sessoes: Iterable[Mapping], simular: bool = False,
            executar: Callable[[list[str]], object] | None = None,
            pausa: float = PAUSA_ENTRE_ABAS) -> Resultado:
    executar = executar or _executar
    abertas, puladas = [], []
    wt = None
    for sessao in sessoes:
        motivo = motivo_para_pular(sessao)
        if motivo:
            puladas.append((sessao, motivo))
            continue
        if not simular:
            wt = wt or localizar_wt()
            if abertas:
                time.sleep(pausa)
            executar(argumentos_wt(wt, distro(), lancador(), sessao))
        abertas.append(sessao)
    return Resultado(abertas, puladas)


def _executar(argumentos: list[str]) -> None:
    try:
        subprocess.run(argumentos, check=True, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    except subprocess.CalledProcessError as e:
        raise ErroReabrir(f"wt.exe falhou ({e.returncode}): {(e.stderr or '').strip()}") from e
    except OSError as e:
        raise ErroReabrir(f"não consegui executar o wt.exe: {e}") from e


# --- dentro da aba ------------------------------------------------------------------


def rodar_na_aba(sessao: Mapping) -> None:
    """Roda o resume da sessão e, quando ele sair, deixa um shell aberto no mesmo diretório.

    O resume passa pelo shell interativo do usuário porque é o .zshrc que põe claude/codex no PATH
    (fnm, ~/.local/bin…) — o wsl.exe não abre um shell interativo por conta própria.
    """
    shell = os.environ.get("SHELL") or pwd.getpwuid(os.getuid()).pw_shell or "/bin/bash"
    comando = provedores.por_nome(sessao["provedor"]).comando_resume(sessao["session_id"])
    os.chdir(sessao["dir_resume"])
    codigo = subprocess.call([shell, "-ic", shlex.join(comando)])
    if codigo != 0:
        print(f"\n[{comando[0]} saiu com código {codigo} — comando: {shlex.join(comando)}]")
    os.execv(shell, [shell, "-i"])
