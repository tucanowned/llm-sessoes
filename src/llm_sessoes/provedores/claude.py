"""Provedor do Claude Code.

Depende de formatos internos (não documentados) do Claude Code:
- ~/.claude/sessions/<pid>.json — um arquivo por processo, com sessionId/cwd/status/kind/procStart;
- ~/.claude/projects/<slug>/<sessionId>.jsonl — a conversa; registros `ai-title`, `last-prompt`
  e os campos `cwd`/`gitBranch` das mensagens se repetem ao longo do arquivo.
Se algo aqui mudar, o estrago fica contido neste módulo.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .. import sistema
from ..modelos import SessaoViva

UUID = re.compile(r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
CAUDA_INICIAL = 256 * 1024
CAUDA_MAXIMA = 8 * 1024 * 1024
LIMITE_MSG = 300


class ProvedorClaude:
    nome = "claude"

    def __init__(self, raiz: Path | None = None, proc: Path = sistema.PROC):
        raiz = raiz or Path.home() / ".claude"
        self.dir_sessoes = raiz / "sessions"
        self.dir_projetos = raiz / "projects"
        self.proc = proc
        self.avisos: list[str] = []

    def comando_resume(self, session_id: str) -> list[str]:
        return ["claude", "--resume", session_id]

    def sessoes_vivas(self) -> list[SessaoViva]:
        self.avisos = []
        vivas: dict[int, SessaoViva] = {}
        pids_com_registro: set[int] = set()

        for arquivo in sorted(self.dir_sessoes.glob("*.json")):
            registro = _ler_json(arquivo)
            if not registro or not isinstance(registro.get("pid"), int):
                continue
            pid = registro["pid"]
            processo = sistema.ler_processo(pid, self.proc)
            if not processo or processo.starttime != str(registro.get("procStart")):
                continue  # arquivo órfão (crash) ou PID reutilizado por outro processo
            pids_com_registro.add(pid)
            if registro.get("kind") != "interactive" or not registro.get("sessionId"):
                continue  # jobs em background / processos "spare" não são sessões do usuário
            vivas[pid] = self._montar(
                session_id=registro["sessionId"],
                pid=pid,
                dir_resume=registro.get("cwd") or processo.cwd or "",
                tty=processo.tty,
                status=registro.get("status"),
                versao=registro.get("version"),
            )

        # Rede de segurança: processo `claude` sem arquivo de sessão (formato mudou?).
        # Só dá para aproveitar se o id estiver na linha de comando.
        for processo in sistema.listar_processos("claude", self.proc):
            if processo.pid in pids_com_registro:
                continue
            if not processo.tty:
                continue
            # Todo `claude` interativo deveria ter registro: se não tem, o formato mudou.
            self.avisos.append(f"processo claude pid={processo.pid} ({processo.tty}) sem registro "
                               f"em {self.dir_sessoes} — formato interno mudou?")
            session_id = _id_da_linha_de_comando(processo.cmdline)
            if session_id:
                vivas[processo.pid] = self._montar(
                    session_id=session_id, pid=processo.pid,
                    dir_resume=processo.cwd or "", tty=processo.tty, status=None,
                )

        return list(vivas.values())

    def _montar(self, session_id: str, pid: int, dir_resume: str, tty: str | None,
                status: str | None, versao: str | None = None) -> SessaoViva:
        meta = self._metadados(session_id)
        return SessaoViva(
            provedor=self.nome, session_id=session_id, pid=pid, dir_resume=dir_resume,
            dir_trabalho=meta.get("cwd"), tty=tty, status=status,
            titulo=meta.get("titulo"), branch=meta.get("branch"), ultima_msg=meta.get("ultima_msg"),
            versao=versao,
        )

    def _metadados(self, session_id: str) -> dict[str, str]:
        if not UUID.match(session_id):
            return {}
        arquivo = next(iter(self.dir_projetos.glob(f"*/{session_id}.jsonl")), None)
        if not arquivo:
            return {}  # sessão recém-aberta, ainda sem conversa gravada

        # As conversas chegam a centenas de MB: lê-se só a cauda, ampliando se o título não vier.
        tamanho = arquivo.stat().st_size
        cauda = CAUDA_INICIAL
        while True:
            meta = _extrair(sistema.ler_cauda(arquivo, cauda))
            if "titulo" in meta or cauda >= min(tamanho, CAUDA_MAXIMA):
                return meta
            cauda *= 4


def _extrair(linhas: list[str]) -> dict[str, str]:
    meta: dict[str, str] = {}
    for linha in linhas:  # em ordem: o último registro de cada tipo prevalece
        try:
            d = json.loads(linha)
        except ValueError:
            continue  # primeira linha da cauda costuma vir cortada
        if not isinstance(d, dict):
            continue
        tipo = d.get("type")
        if tipo == "ai-title" and d.get("aiTitle"):
            meta["titulo_ia"] = d["aiTitle"]
        elif tipo == "custom-title" and d.get("customTitle"):
            meta["titulo_custom"] = d["customTitle"]
        elif tipo == "last-prompt" and d.get("lastPrompt"):
            meta["ultima_msg"] = " ".join(str(d["lastPrompt"]).split())[:LIMITE_MSG]
        if d.get("cwd"):
            meta["cwd"] = d["cwd"]
        if d.get("gitBranch"):
            meta["branch"] = d["gitBranch"]

    titulo = meta.pop("titulo_custom", None) or meta.get("titulo_ia")
    meta.pop("titulo_ia", None)
    if titulo:
        meta["titulo"] = titulo
    return meta


def _ler_json(arquivo: Path) -> dict | None:
    try:
        dado = json.loads(arquivo.read_text())
    except (OSError, ValueError):
        return None
    return dado if isinstance(dado, dict) else None


def _id_da_linha_de_comando(cmdline: list[str]) -> str | None:
    for i, arg in enumerate(cmdline):
        if arg in ("--resume", "-r") and i + 1 < len(cmdline) and UUID.match(cmdline[i + 1]):
            return cmdline[i + 1]
        if arg.startswith("--resume=") and UUID.match(arg.split("=", 1)[1]):
            return arg.split("=", 1)[1]
    return None
