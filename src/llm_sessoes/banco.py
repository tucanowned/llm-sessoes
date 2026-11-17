"""Persistência em SQLite. Horários são epoch em segundos (UTC)."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .modelos import SessaoViva

ESQUEMA = """
CREATE TABLE IF NOT EXISTS sessoes (
    provedor      TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    titulo        TEXT,
    dir_resume    TEXT NOT NULL,
    dir_trabalho  TEXT,
    branch        TEXT,
    ultima_msg    TEXT,
    primeira_vez  INTEGER NOT NULL,
    ultima_vez    INTEGER NOT NULL,
    ultima_atividade INTEGER,           -- última escrita na conversa, quando o provedor sabe
    PRIMARY KEY (provedor, session_id)
);

-- Estado da última coleta: quem estava vivo.
CREATE TABLE IF NOT EXISTS vivas (
    provedor    TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    pid         INTEGER NOT NULL,
    tty         TEXT,
    status      TEXT,
    desde       INTEGER NOT NULL,
    tmux        TEXT,                   -- JSON {sessao, janela, painel} ou NULL
    PRIMARY KEY (provedor, session_id)
);

CREATE TABLE IF NOT EXISTS eventos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    quando      INTEGER NOT NULL,
    tipo        TEXT NOT NULL,          -- abriu | fechou | crash
    provedor    TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    pid         INTEGER,
    tty         TEXT,
    boot_id     TEXT
);
CREATE INDEX IF NOT EXISTS eventos_tipo_quando ON eventos (tipo, quando);

CREATE TABLE IF NOT EXISTS meta (
    chave  TEXT PRIMARY KEY,
    valor  TEXT NOT NULL
);
"""

COLUNAS_NOVAS = {
    # tabela: [(coluna, tipo)] — acrescentadas em bancos criados antes delas existirem
    "sessoes": [("ultima_atividade", "INTEGER")],
    "vivas": [("tmux", "TEXT")],
}


def caminho_padrao() -> Path:
    if os.environ.get("LLM_SESSOES_DB"):
        return Path(os.environ["LLM_SESSOES_DB"])
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    return base / "llm-sessoes" / "sessoes.db"


class Banco:
    def __init__(self, caminho: Path | str):
        if str(caminho) != ":memory:":
            Path(caminho).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(caminho), timeout=10)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(ESQUEMA)
        self._migrar()

    def _migrar(self) -> None:
        for tabela, colunas in COLUNAS_NOVAS.items():
            existentes = {l["name"] for l in self.con.execute(f"PRAGMA table_info({tabela})")}
            for coluna, tipo in colunas:
                if coluna not in existentes:
                    self.con.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo}")
        self.con.commit()

    def fechar(self) -> None:
        self.con.close()

    @contextmanager
    def transacao(self) -> Iterator[None]:
        """Transação que já nasce com o lock de escrita (BEGIN IMMEDIATE).

        O `with con:` do sqlite3 só abre a transação no primeiro INSERT/UPDATE: as leituras
        anteriores ficam fora do lock, e dois processos simultâneos (ex.: vários shells rodando
        `aviso` logo após o boot) decidem em cima do mesmo estado e duplicam eventos.
        """
        self.con.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.con.rollback()
            raise
        self.con.commit()

    # --- meta -----------------------------------------------------------------

    def meta(self, chave: str) -> str | None:
        linha = self.con.execute("SELECT valor FROM meta WHERE chave = ?", (chave,)).fetchone()
        return linha["valor"] if linha else None

    def definir_meta(self, chave: str, valor: str) -> None:
        self.con.execute(
            "INSERT INTO meta (chave, valor) VALUES (?, ?) "
            "ON CONFLICT (chave) DO UPDATE SET valor = excluded.valor",
            (chave, valor),
        )

    # --- escrita (usada pelo coletor) -------------------------------------------

    def salvar_sessao(self, s: SessaoViva, agora: int) -> None:
        # COALESCE: metadado que o provedor não conseguiu ler nesta rodada não apaga o já conhecido.
        self.con.execute(
            """
            INSERT INTO sessoes (provedor, session_id, titulo, dir_resume, dir_trabalho,
                                 branch, ultima_msg, primeira_vez, ultima_vez, ultima_atividade)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (provedor, session_id) DO UPDATE SET
                titulo       = COALESCE(excluded.titulo, titulo),
                dir_resume   = excluded.dir_resume,
                dir_trabalho = COALESCE(excluded.dir_trabalho, dir_trabalho),
                branch       = COALESCE(excluded.branch, branch),
                ultima_msg   = COALESCE(excluded.ultima_msg, ultima_msg),
                ultima_vez   = excluded.ultima_vez,
                ultima_atividade = COALESCE(excluded.ultima_atividade, ultima_atividade)
            """,
            (s.provedor, s.session_id, s.titulo, s.dir_resume, s.dir_trabalho,
             s.branch, s.ultima_msg, agora, agora, s.ultima_atividade),
        )

    def salvar_viva(self, s: SessaoViva, agora: int, tmux: dict | None = None) -> None:
        self.con.execute(
            """
            INSERT INTO vivas (provedor, session_id, pid, tty, status, desde, tmux)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (provedor, session_id) DO UPDATE SET
                pid = excluded.pid, tty = excluded.tty, status = excluded.status,
                tmux = excluded.tmux
            """,
            (s.provedor, s.session_id, s.pid, s.tty, s.status, agora,
             json.dumps(tmux) if tmux else None),
        )

    def remover_viva(self, provedor: str, session_id: str) -> None:
        self.con.execute(
            "DELETE FROM vivas WHERE provedor = ? AND session_id = ?", (provedor, session_id)
        )

    def registrar_evento(self, quando: int, tipo: str, provedor: str, session_id: str,
                         pid: int | None, tty: str | None, boot_id: str | None) -> None:
        self.con.execute(
            "INSERT INTO eventos (quando, tipo, provedor, session_id, pid, tty, boot_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (quando, tipo, provedor, session_id, pid, tty, boot_id),
        )

    def fechou_desde(self, provedor: str, session_id: str, quando: int) -> bool:
        return self.con.execute(
            "SELECT 1 FROM eventos WHERE tipo = 'fechou' AND provedor = ? AND session_id = ? "
            "AND quando >= ? LIMIT 1",
            (provedor, session_id, quando),
        ).fetchone() is not None

    def expurgar_eventos(self, antes_de: int) -> None:
        self.con.execute("DELETE FROM eventos WHERE quando < ?", (antes_de,))

    # --- leitura ----------------------------------------------------------------

    def vivas(self) -> list[sqlite3.Row]:
        return self.con.execute(
            """
            SELECT v.pid, v.tty, v.status, v.desde, v.tmux, s.*
            FROM vivas v JOIN sessoes s USING (provedor, session_id)
            ORDER BY v.desde, v.pid
            """
        ).fetchall()

    def eventos_desde(self, desde: int) -> list[sqlite3.Row]:
        """Eventos a partir de `desde`, com os metadados da sessão, do mais antigo ao mais novo."""
        return self.con.execute(
            """
            SELECT e.tipo, e.quando, s.*
            FROM eventos e JOIN sessoes s USING (provedor, session_id)
            WHERE e.quando >= ?
            ORDER BY e.quando, e.id
            """,
            (desde,),
        ).fetchall()

    def fechadas(self, desde: int) -> list[sqlite3.Row]:
        """Sessões fechadas a partir de `desde` que não estão vivas agora (não foram reabertas)."""
        return self.con.execute(
            """
            SELECT MAX(e.quando) AS quando, e.tty, s.*
            FROM eventos e JOIN sessoes s USING (provedor, session_id)
            WHERE e.tipo = 'fechou' AND e.quando >= ?
              AND NOT EXISTS (SELECT 1 FROM vivas v
                              WHERE v.provedor = e.provedor AND v.session_id = e.session_id)
            GROUP BY e.provedor, e.session_id
            ORDER BY quando DESC
            """,
            (desde,),
        ).fetchall()

    def ultimo_crash(self) -> list[sqlite3.Row]:
        """Sessões do crash mais recente (todas as que morreram no mesmo boot)."""
        return self.con.execute(
            """
            SELECT MIN(e.id) AS primeiro, e.quando, e.tty, s.*,
                   EXISTS (SELECT 1 FROM vivas v
                           WHERE v.provedor = e.provedor AND v.session_id = e.session_id) AS reaberta
            FROM eventos e JOIN sessoes s USING (provedor, session_id)
            WHERE e.tipo = 'crash'
              AND e.boot_id = (SELECT boot_id FROM eventos WHERE tipo = 'crash'
                               ORDER BY quando DESC, id DESC LIMIT 1)
            GROUP BY e.provedor, e.session_id  -- uma linha por sessão, mesmo com evento repetido
            ORDER BY primeiro
            """
        ).fetchall()

    def por_prefixo(self, prefixo: str) -> list[sqlite3.Row]:
        """Sessões cujo id começa com `prefixo`, com a coluna `viva`."""
        escapado = prefixo.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return self.con.execute(
            """
            SELECT s.*,
                   EXISTS (SELECT 1 FROM vivas v
                           WHERE v.provedor = s.provedor AND v.session_id = s.session_id) AS viva
            FROM sessoes s
            WHERE s.session_id LIKE ? ESCAPE '\\'
            ORDER BY s.ultima_vez DESC
            """,
            (escapado + "%",),
        ).fetchall()

    def todas(self) -> list[sqlite3.Row]:
        """Todas as sessões conhecidas, com `viva` e o tipo do último evento (`ultimo_evento`)."""
        return self.con.execute(
            """
            SELECT s.*,
                   EXISTS (SELECT 1 FROM vivas v
                           WHERE v.provedor = s.provedor AND v.session_id = s.session_id) AS viva,
                   (SELECT e.tipo FROM eventos e
                    WHERE e.provedor = s.provedor AND e.session_id = s.session_id
                    ORDER BY e.quando DESC, e.id DESC LIMIT 1) AS ultimo_evento
            FROM sessoes s
            ORDER BY s.ultima_vez DESC
            """
        ).fetchall()
