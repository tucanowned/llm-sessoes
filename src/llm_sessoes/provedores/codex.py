"""Provedor do Codex CLI.

Depende de formatos internos (não documentados) do Codex — validado na versão 0.154:
- o processo `codex` mantém aberto um ~/.codex/thread-writer-locks/<thread-id>.lock por thread
  em que escreve (a conversa e threads auxiliares) e o rollout-*.jsonl da conversa;
- ~/.codex/state_<N>.sqlite, tabela `threads`, tem os metadados (cwd, name, title, git_branch,
  thread_source, archived). A thread só entra ali depois da primeira mensagem;
- ~/.codex/sessions/AAAA/MM/DD/rollout-<data>-<thread-id>.jsonl: primeira linha `session_meta`;
  mensagens do usuário são `response_item` com role=user (contexto injetado começa com '<').
Se algo aqui mudar, o estrago fica contido neste módulo.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from .. import sistema
from ..modelos import SessaoViva

_UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
LOCK = re.compile(rf"/thread-writer-locks/({_UUID})\.lock$")
ROLLOUT = re.compile(rf"/sessions/.*rollout-.*-({_UUID})\.jsonl$")
CAUDA = 512 * 1024
LIMITE_TITULO = 80
LIMITE_MSG = 300


class ProvedorCodex:
    nome = "codex"

    def __init__(self, raiz: Path | None = None, proc: Path = sistema.PROC):
        self.raiz = raiz or Path.home() / ".codex"
        self.proc = proc
        self.avisos: list[str] = []

    def comando_resume(self, session_id: str) -> list[str]:
        return ["codex", "resume", session_id]

    def sessoes_vivas(self) -> list[SessaoViva]:
        self.avisos = []
        vivas = []
        for processo in sistema.listar_processos("codex", self.proc):
            if not processo.tty:
                continue  # `codex exec`, servidores MCP etc. não são sessões interativas
            abertos = sistema.arquivos_abertos(processo.pid, self.proc)
            rollouts = {m.group(1): Path(a) for a in abertos if (m := ROLLOUT.search(a))}
            ids = {m.group(1) for a in abertos if (m := LOCK.search(a))} | set(rollouts)
            if not ids:
                # Mesmo sem nenhuma mensagem o codex já segura os locks: zero ids é anomalia.
                self.avisos.append(f"processo codex pid={processo.pid} ({processo.tty}) sem "
                                   f"thread-writer-lock nem rollout aberto — formato interno mudou?")

            for thread_id in sorted(ids):
                meta = self._thread(thread_id) or _meta_do_rollout(rollouts.get(thread_id))
                if not meta or meta.get("thread_source") != "user" or meta.get("archived"):
                    continue  # thread auxiliar/subagente, arquivada ou ainda sem conversa
                rollout = rollouts.get(thread_id) or _caminho(meta.get("rollout_path"))
                vivas.append(SessaoViva(
                    provedor=self.nome, session_id=thread_id, pid=processo.pid,
                    dir_resume=processo.cwd or meta.get("cwd") or "",
                    dir_trabalho=meta.get("cwd"), tty=processo.tty,
                    titulo=_titulo(meta), branch=meta.get("git_branch"),
                    ultima_msg=_ultima_mensagem(rollout),
                    versao=meta.get("cli_version"),
                ))
        return vivas

    def _thread(self, thread_id: str) -> dict | None:
        banco = self._banco_de_estado()
        if not banco:
            return None
        try:
            con = sqlite3.connect(f"file:{banco}?mode=ro", uri=True, timeout=2)
            con.row_factory = sqlite3.Row
            try:
                linha = con.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
            finally:
                con.close()
        except sqlite3.Error as erro:
            aviso = f"não consegui ler threads em {banco.name} ({erro}) — esquema mudou?"
            if aviso not in self.avisos:
                self.avisos.append(aviso)
            return None  # cai no session_meta do rollout
        return dict(linha) if linha else None

    def _banco_de_estado(self) -> Path | None:
        """O nome carrega a versão do esquema (state_5.sqlite…): usa-se a mais alta."""
        def versao(p: Path) -> int:
            m = re.fullmatch(r"state_(\d+)\.sqlite", p.name)
            return int(m.group(1)) if m else -1

        candidatos = [p for p in self.raiz.glob("state_*.sqlite") if versao(p) >= 0]
        return max(candidatos, key=versao, default=None)


def _titulo(meta: dict) -> str | None:
    titulo = meta.get("name") or meta.get("title") or meta.get("first_user_message")
    return " ".join(str(titulo).split())[:LIMITE_TITULO] if titulo else None


def _caminho(valor) -> Path | None:
    return Path(valor) if valor else None


def _meta_do_rollout(rollout: Path | None) -> dict | None:
    if not rollout:
        return None
    try:
        with rollout.open(errors="replace") as f:
            d = json.loads(f.readline())
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("type") != "session_meta":
        return None
    payload = d.get("payload")
    return payload if isinstance(payload, dict) else None


def _ultima_mensagem(rollout: Path | None) -> str | None:
    if not rollout:
        return None
    try:
        linhas = sistema.ler_cauda(rollout, CAUDA)
    except OSError:
        return None
    for linha in reversed(linhas):
        if '"role":"user"' not in linha.replace(" ", ""):
            continue  # evita json.loads em linhas enormes que não interessam
        try:
            payload = json.loads(linha).get("payload") or {}
        except (ValueError, AttributeError):
            continue
        if payload.get("type") != "message" or payload.get("role") != "user":
            continue
        for bloco in payload.get("content") or []:
            texto = (bloco.get("text") or "").strip() if isinstance(bloco, dict) else ""
            if texto and not texto.startswith("<"):
                return " ".join(texto.split())[:LIMITE_MSG]
    return None
