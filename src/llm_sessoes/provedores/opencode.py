"""Provedor do opencode.

Diferente do Claude Code e do Codex, a sessão do opencode não pertence a um processo: o TUI é só
um cliente do serviço em segundo plano (`opencode serve --service`), e nada no /proc diz qual
sessão cada TUI está mostrando. Por isso "aberta" aqui quer dizer **em andamento**: sessão
principal, não arquivada, com atividade nos últimos `dias` dias. Ela fecha quando:
- fica mais que isso sem atividade (o coletor data o `fechou` da última atividade);
- é arquivada no opencode;
- o usuário fecha à mão: `llm-sessoes opencode fechar <id>`.

Depende de formatos internos (não documentados) — validado no opencode 2.0.29:
- ~/.local/share/opencode/opencode.db, tabela `session_v2` (id `ses_…`, title, directory,
  project_id, parent_id, time_updated/time_idle/time_archived em **milissegundos**, version);
- `project.worktree` é a raiz do repositório (`/` no projeto "global");
- `session_message`: type='user' traz o texto em `data.text`; ao fim de cada turno o opencode
  grava uma linha type='idle' — se ela é a mais recente, a sessão está parada;
- `time_updated` também muda quando a sessão só é aberta no TUI (não só com mensagem): abrir
  conta como atividade, o que é o desejado para a regra dos `dias`.
A tabela `session` antiga (v1) foi migrada para a `session_v2` e fica vazia.
Se algo aqui mudar, o estrago fica contido neste módulo.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from ..modelos import SessaoViva

DIAS_PADRAO = 10
LIMITE_MSG = 300
LIMITE_TITULO = 80

CONSULTA = """
SELECT s.id, s.title, s.directory, s.version, s.time_updated, p.worktree
FROM session_v2 s LEFT JOIN project p ON p.id = s.project_id
WHERE s.parent_id IS NULL            -- subsessões (subagentes) não são sessões do usuário
  AND s.time_archived IS NULL
  AND s.time_updated >= ?
ORDER BY s.time_updated
"""


class ProvedorOpencode:
    nome = "opencode"
    persistente = True
    """A sessão vive no serviço do opencode, não num processo: um reboot não é crash, e o
    fechamento por inatividade é datado da última atividade."""

    def __init__(self, raiz: Path | None = None, dias: float | None = None,
                 relogio: Callable[[], float] = time.time):
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
        self.banco = (raiz or base / "opencode") / "opencode.db"
        if dias is None:
            dias = float(os.environ.get("LLM_SESSOES_OPENCODE_DIAS") or DIAS_PADRAO)
        self.dias = dias
        self.relogio = relogio
        self.avisos: list[str] = []

    def comando_resume(self, session_id: str) -> list[str]:
        return ["opencode", "-s", session_id]

    def sessoes_vivas(self) -> list[SessaoViva]:
        self.avisos = []
        if not self.banco.exists():
            return []  # opencode não instalado (ou nunca usado) nesta máquina
        limite_ms = int((self.relogio() - self.dias * 86400) * 1000)
        try:
            con = sqlite3.connect(f"file:{self.banco}?mode=ro", uri=True, timeout=2)
            con.row_factory = sqlite3.Row
            try:
                linhas = con.execute(CONSULTA, (limite_ms,)).fetchall()
                msgs = {l["id"]: _ultima_mensagem(con, l["id"]) for l in linhas}
                status = {l["id"]: _status(con, l["id"]) for l in linhas}
            finally:
                con.close()
        except sqlite3.Error as erro:
            # Esquema mudou: aviso (e o coletor não registra fechamentos), nunca lista vazia muda.
            self.avisos.append(f"não consegui ler session_v2 em {self.banco.name} ({erro}) "
                               f"— esquema mudou?")
            return []

        vivas = []
        for l in linhas:
            raiz = l["worktree"] if l["worktree"] and l["worktree"] != "/" else None
            vivas.append(SessaoViva(
                provedor=self.nome, session_id=l["id"], pid=0, tty=None,
                dir_resume=raiz or l["directory"], dir_trabalho=l["directory"],
                status=status.get(l["id"]),
                titulo=" ".join(l["title"].split())[:LIMITE_TITULO] if l["title"] else None,
                branch=_branch(l["directory"]), ultima_msg=msgs.get(l["id"]),
                versao=l["version"], ultima_atividade=l["time_updated"] // 1000,
            ))
        return vivas


def _status(con: sqlite3.Connection, session_id: str) -> str | None:
    """`idle` se o último registro é o fim de turno; `busy` se veio mensagem depois dele."""
    linha = con.execute("SELECT type FROM session_message WHERE session_id = ? "
                        "ORDER BY seq DESC LIMIT 1", (session_id,)).fetchone()
    if linha is None:
        return None  # sessão sem nenhuma mensagem ainda
    return "idle" if linha["type"] == "idle" else "busy"


def _ultima_mensagem(con: sqlite3.Connection, session_id: str) -> str | None:
    linha = con.execute(
        "SELECT data FROM session_message WHERE session_id = ? AND type = 'user' "
        "ORDER BY seq DESC LIMIT 1", (session_id,)).fetchone()
    try:
        texto = json.loads(linha["data"]).get("text") if linha else None
    except (ValueError, AttributeError):
        return None
    return " ".join(texto.split())[:LIMITE_MSG] if isinstance(texto, str) and texto.strip() else None


def _branch(diretorio: str | None) -> str | None:
    """Branch do git lida direto do HEAD (o opencode não guarda). Funciona em worktree, onde o
    `.git` é um arquivo `gitdir: …` apontando para o HEAD próprio daquele worktree."""
    if not diretorio:
        return None
    git = Path(diretorio) / ".git"
    try:
        if git.is_file():
            alvo = git.read_text().strip().removeprefix("gitdir:").strip()
            git = (Path(diretorio) / alvo).resolve()
        head = (git / "HEAD").read_text().strip()
    except OSError:
        return None
    return head.removeprefix("ref: refs/heads/") if head.startswith("ref: refs/heads/") else None
