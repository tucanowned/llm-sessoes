"""SQLite do servidor central. Horários são epoch em segundos (UTC), como no cliente."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

OFFLINE_APOS = 5 * 60
"""Máquina sem relatório há mais que isso é mostrada como offline."""
RETENCAO_EVENTOS = 30 * 24 * 3600
JANELA_RECENTES = 48 * 3600
TIPOS_EVENTO = ("abriu", "fechou", "crash")
NOME_MAQUINA = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
"""O nome vira alvo do `ssh` no comando copiado: só o que é seguro num hostname."""

ESQUEMA = """
CREATE TABLE IF NOT EXISTS maquinas (
    nome            TEXT PRIMARY KEY,
    boot_id         TEXT,
    coletado_em     INTEGER,            -- relógio da máquina
    recebido_em     INTEGER NOT NULL,   -- relógio do servidor: é o que decide online/offline
    versao_cliente  TEXT,
    anomalias       TEXT NOT NULL DEFAULT '[]'
);

-- Substituída por inteiro a cada relatório da máquina.
CREATE TABLE IF NOT EXISTS vivas (
    maquina           TEXT NOT NULL,
    provedor          TEXT NOT NULL,
    session_id        TEXT NOT NULL,
    titulo            TEXT,
    ultima_msg        TEXT,
    dir_resume        TEXT,
    dir_trabalho      TEXT,
    branch            TEXT,
    status            TEXT,
    inicio            INTEGER,
    ultima_atividade  INTEGER,
    tmux              TEXT,             -- JSON {sessao, janela, painel} ou NULL
    PRIMARY KEY (maquina, provedor, session_id)
);

-- Acumula: o cliente manda só 48 h, o servidor guarda até RETENCAO_EVENTOS.
CREATE TABLE IF NOT EXISTS eventos (
    maquina           TEXT NOT NULL,
    tipo              TEXT NOT NULL,
    quando            INTEGER NOT NULL,
    provedor          TEXT NOT NULL,
    session_id        TEXT NOT NULL,
    titulo            TEXT,
    ultima_msg        TEXT,
    dir_resume        TEXT,
    dir_trabalho      TEXT,
    branch            TEXT,
    inicio            INTEGER,
    ultima_atividade  INTEGER,
    PRIMARY KEY (maquina, provedor, session_id, tipo, quando)
);
CREATE INDEX IF NOT EXISTS eventos_quando ON eventos (quando);
"""

CAMPOS_SESSAO = ("titulo", "ultima_msg", "dir_resume", "dir_trabalho", "branch",
                 "inicio", "ultima_atividade")
LIMITES_TEXTO = {"titulo": 500, "ultima_msg": 300, "dir_resume": 1000, "dir_trabalho": 1000,
                 "branch": 300, "status": 30, "provedor": 30, "id": 200}


class RelatorioInvalido(ValueError):
    pass


class Armazem:
    def __init__(self, caminho: Path | str):
        if str(caminho) != ":memory:":
            Path(caminho).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(caminho), timeout=10, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        if str(caminho) != ":memory:":
            self.con.execute("PRAGMA journal_mode=WAL")  # leitura da página não espera a escrita
        self.con.executescript(ESQUEMA)

    def fechar(self) -> None:
        self.con.close()

    # --- escrita ----------------------------------------------------------------

    def gravar(self, relatorio: dict, recebido_em: int) -> None:
        """Substitui o estado da máquina pelo retrato recebido (já validado)."""
        r = relatorio
        maquina = r["maquina"]
        with self.con:
            self.con.execute(
                """
                INSERT INTO maquinas (nome, boot_id, coletado_em, recebido_em, versao_cliente,
                                      anomalias)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (nome) DO UPDATE SET
                    boot_id = excluded.boot_id, coletado_em = excluded.coletado_em,
                    recebido_em = excluded.recebido_em, versao_cliente = excluded.versao_cliente,
                    anomalias = excluded.anomalias
                """,
                (maquina, r["boot_id"], r["coletado_em"], recebido_em, r["versao_cliente"],
                 json.dumps(r["anomalias"], ensure_ascii=False)),
            )
            self.con.execute("DELETE FROM vivas WHERE maquina = ?", (maquina,))
            self.con.executemany(
                f"""
                INSERT OR REPLACE INTO vivas (maquina, provedor, session_id, status, tmux,
                                              {", ".join(CAMPOS_SESSAO)})
                VALUES (?, ?, ?, ?, ?, {", ".join("?" * len(CAMPOS_SESSAO))})
                """,
                [(maquina, s["provedor"], s["id"], s["status"],
                  json.dumps(s["tmux"], ensure_ascii=False) if s["tmux"] else None,
                  *(s[c] for c in CAMPOS_SESSAO))
                 for s in r["vivas"]],
            )
            self.con.executemany(
                f"""
                INSERT OR REPLACE INTO eventos (maquina, tipo, quando, provedor, session_id,
                                                {", ".join(CAMPOS_SESSAO)})
                VALUES (?, ?, ?, ?, ?, {", ".join("?" * len(CAMPOS_SESSAO))})
                """,
                [(maquina, e["tipo"], e["quando"], e["provedor"], e["id"],
                  *(e[c] for c in CAMPOS_SESSAO))
                 for e in r["eventos"]],
            )
            self.con.execute("DELETE FROM eventos WHERE quando < ?",
                             (recebido_em - RETENCAO_EVENTOS,))

    # --- leitura ----------------------------------------------------------------

    def painel(self, agora: int) -> list[dict]:
        """Uma entrada por máquina (online primeiro), com sessões vivas e recentes."""
        maquinas = []
        for m in self.con.execute("SELECT * FROM maquinas ORDER BY nome"):
            maquinas.append({
                "nome": m["nome"],
                "online": online(m["recebido_em"], agora),
                "recebido_em": m["recebido_em"],
                "versao_cliente": m["versao_cliente"],
                "anomalias": _lista(m["anomalias"]),
                "vivas": self._vivas(m["nome"]),
                "recentes": self._recentes(m["nome"], agora - JANELA_RECENTES),
            })
        maquinas.sort(key=lambda m: not m["online"])  # estável: dentro do grupo, por nome
        return maquinas

    def resumo(self, agora: int) -> dict:
        """Para o widget `customapi` do Homepage."""
        limite = agora - OFFLINE_APOS
        um = lambda sql, *p: self.con.execute(sql, p).fetchone()[0]  # noqa: E731
        return {
            # Sessões de máquina offline são um retrato velho: não contam como ativas.
            "ativas": um("SELECT COUNT(*) FROM vivas v JOIN maquinas m ON m.nome = v.maquina "
                         "WHERE m.recebido_em >= ?", limite),
            "maquinas_online": um("SELECT COUNT(*) FROM maquinas WHERE recebido_em >= ?", limite),
            "maquinas_total": um("SELECT COUNT(*) FROM maquinas"),
            "crashes_24h": um("SELECT COUNT(DISTINCT maquina || ' ' || provedor || ' ' || "
                              "session_id) FROM eventos WHERE tipo = 'crash' AND quando >= ?",
                              agora - 24 * 3600),
        }

    def _vivas(self, maquina: str) -> list[dict]:
        linhas = self.con.execute(
            "SELECT * FROM vivas WHERE maquina = ? "
            "ORDER BY COALESCE(ultima_atividade, inicio) DESC", (maquina,))
        return [{**dict(l), "tmux": _objeto(l["tmux"])} for l in linhas]

    def _recentes(self, maquina: str, desde: int) -> list[dict]:
        """Fechadas/crash na janela, o evento mais recente de cada sessão, sem as reabertas."""
        linhas = self.con.execute(
            """
            SELECT e.* FROM eventos e
            WHERE e.maquina = ? AND e.tipo IN ('fechou', 'crash') AND e.quando >= ?
              AND e.quando = (SELECT MAX(x.quando) FROM eventos x
                              WHERE x.maquina = e.maquina AND x.provedor = e.provedor
                                AND x.session_id = e.session_id AND x.tipo IN ('fechou', 'crash'))
              AND NOT EXISTS (SELECT 1 FROM vivas v WHERE v.maquina = e.maquina
                              AND v.provedor = e.provedor AND v.session_id = e.session_id)
            GROUP BY e.provedor, e.session_id
            ORDER BY e.quando DESC
            """,
            (maquina, desde))
        return [dict(l) for l in linhas]


def online(recebido_em: int | None, agora: int) -> bool:
    return recebido_em is not None and agora - recebido_em <= OFFLINE_APOS


# --- validação do relatório ----------------------------------------------------------


def validar(dado) -> dict:
    """Confere o formato (versao_esquema 1) e devolve uma cópia normalizada.

    Tudo que vem do cliente é tratado como não confiável: tipos conferidos e textos truncados.
    """
    if not isinstance(dado, dict):
        raise RelatorioInvalido("o corpo precisa ser um objeto JSON")
    if dado.get("versao_esquema") != 1:
        raise RelatorioInvalido(f"versao_esquema não suportada: {dado.get('versao_esquema')!r}")
    maquina = dado.get("maquina")
    if not isinstance(maquina, str) or not NOME_MAQUINA.match(maquina):
        raise RelatorioInvalido("maquina inválida (letras, números, '.', '_' e '-'; até 64)")
    for chave in ("vivas", "eventos", "anomalias"):
        if not isinstance(dado.get(chave, []), list):
            raise RelatorioInvalido(f"{chave} precisa ser uma lista")

    eventos = []
    for e in dado.get("eventos", []):
        if not isinstance(e, dict) or e.get("tipo") not in TIPOS_EVENTO:
            raise RelatorioInvalido("evento inválido")
        eventos.append({"tipo": e["tipo"], "quando": _inteiro(e.get("quando"), "quando"),
                        **_sessao(e)})
    return {
        "maquina": maquina,
        "boot_id": _texto(dado.get("boot_id"), 100),
        "coletado_em": _inteiro(dado.get("coletado_em"), "coletado_em", obrigatorio=False),
        "versao_cliente": _texto(dado.get("versao_cliente"), 50),
        "vivas": [_sessao(s) for s in dado.get("vivas", [])],
        "eventos": eventos,
        "anomalias": [_texto(a, 1000) for a in dado.get("anomalias", []) if isinstance(a, str)],
    }


def _sessao(s) -> dict:
    if not isinstance(s, dict):
        raise RelatorioInvalido("sessão precisa ser um objeto")
    provedor, session_id = _texto(s.get("provedor"), 30), _texto(s.get("id"), 200)
    if not provedor or not session_id:
        raise RelatorioInvalido("sessão sem provedor ou id")
    tmux = s.get("tmux")
    if isinstance(tmux, dict) and isinstance(tmux.get("sessao"), str) and tmux["sessao"]:
        tmux = {"sessao": _texto(tmux["sessao"], 100),
                "janela": tmux.get("janela") if isinstance(tmux.get("janela"), int) else None,
                "painel": _texto(tmux.get("painel"), 20)}
    else:
        tmux = None
    return {
        "provedor": provedor, "id": session_id,
        **{c: _texto(s.get(c), LIMITES_TEXTO[c])
           for c in ("titulo", "ultima_msg", "dir_resume", "dir_trabalho", "branch", "status")},
        "inicio": _inteiro(s.get("inicio"), "inicio", obrigatorio=False),
        "ultima_atividade": _inteiro(s.get("ultima_atividade"), "ultima_atividade",
                                     obrigatorio=False),
        "tmux": tmux,
    }


def _texto(valor, limite: int) -> str | None:
    return valor[:limite] if isinstance(valor, str) and valor else None


def _inteiro(valor, nome: str, obrigatorio: bool = True) -> int | None:
    if valor is None and not obrigatorio:
        return None
    # Faixa plausível de epoch: barra NaN/infinito e inteiros que estourariam o SQLite.
    if isinstance(valor, bool) or not isinstance(valor, (int, float)) or not 0 <= valor < 1e11:
        raise RelatorioInvalido(f"{nome} precisa ser um epoch numérico")
    return int(valor)


def _lista(texto: str | None) -> list:
    valor = _objeto(texto)
    return valor if isinstance(valor, list) else []


def _objeto(texto: str | None):
    try:
        return json.loads(texto) if texto else None
    except ValueError:
        return None
