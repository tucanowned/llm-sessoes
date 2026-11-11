"""Persistência em SQLite. Horários são epoch em segundos (UTC)."""

from __future__ import annotations

import os
import sqlite3
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

    def fechar(self) -> None:
        self.con.close()

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
                                 branch, ultima_msg, primeira_vez, ultima_vez)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (provedor, session_id) DO UPDATE SET
                titulo       = COALESCE(excluded.titulo, titulo),
                dir_resume   = excluded.dir_resume,
                dir_trabalho = COALESCE(excluded.dir_trabalho, dir_trabalho),
                branch       = COALESCE(excluded.branch, branch),
                ultima_msg   = COALESCE(excluded.ultima_msg, ultima_msg),
                ultima_vez   = excluded.ultima_vez
            """,
            (s.provedor, s.session_id, s.titulo, s.dir_resume, s.dir_trabalho,
             s.branch, s.ultima_msg, agora, agora),
        )

    def salvar_viva(self, s: SessaoViva, agora: int) -> None:
        self.con.execute(
            """
            INSERT INTO vivas (provedor, session_id, pid, tty, status, desde)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (provedor, session_id) DO UPDATE SET
                pid = excluded.pid, tty = excluded.tty, status = excluded.status
            """,
            (s.provedor, s.session_id, s.pid, s.tty, s.status, agora),
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

    def expurgar_eventos(self, antes_de: int) -> None:
        self.con.execute("DELETE FROM eventos WHERE quando < ?", (antes_de,))

    # --- leitura ----------------------------------------------------------------

    def vivas(self) -> list[sqlite3.Row]:
        return self.con.execute(
            """
            SELECT v.pid, v.tty, v.status, v.desde, s.*
            FROM vivas v JOIN sessoes s USING (provedor, session_id)
            ORDER BY v.desde, v.pid
            """
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
            SELECT e.quando, e.tty, s.*,
                   EXISTS (SELECT 1 FROM vivas v
                           WHERE v.provedor = e.provedor AND v.session_id = e.session_id) AS reaberta
            FROM eventos e JOIN sessoes s USING (provedor, session_id)
            WHERE e.tipo = 'crash'
              AND e.boot_id = (SELECT boot_id FROM eventos WHERE tipo = 'crash'
                               ORDER BY quando DESC, id DESC LIMIT 1)
            ORDER BY e.id
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
