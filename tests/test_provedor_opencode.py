import json
import sqlite3
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from llm_sessoes.provedores.opencode import ProvedorOpencode

AGORA = 1_800_000_000
DIA = 86400

ESQUEMA = """
CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT NOT NULL);
CREATE TABLE session_v2 (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT,
    directory TEXT NOT NULL, title TEXT, version TEXT NOT NULL, time_updated INTEGER NOT NULL,
    time_idle INTEGER, time_archived INTEGER);
CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL,
    seq INTEGER NOT NULL, data TEXT NOT NULL);
"""


class ProvedorOpencodeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raiz = Path(self.tmp.name) / "opencode"
        self.raiz.mkdir()
        self.con = sqlite3.connect(self.raiz / "opencode.db")
        self.con.executescript(ESQUEMA)
        self.con.execute("INSERT INTO project VALUES ('p1', '/home/d/repo'), ('global', '/')")
        self.provedor = ProvedorOpencode(raiz=self.raiz, dias=10, relogio=lambda: AGORA)
        self.seq = 0

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def sessao(self, id_, atividade=AGORA - 3600, projeto="p1", directory="/home/d/repo",
               title="Minha sessão", **extra):
        campos = {"id": id_, "project_id": projeto, "directory": directory, "title": title,
                  "version": "2.0.29", "time_updated": atividade * 1000, **extra}
        self.con.execute(f"INSERT INTO session_v2 ({', '.join(campos)}) "
                         f"VALUES ({', '.join('?' * len(campos))})", list(campos.values()))
        self.con.commit()

    def mensagem(self, session_id, tipo, texto=None):
        self.seq += 1
        dados = {"text": texto} if texto is not None else {}
        self.con.execute("INSERT INTO session_message VALUES (?, ?, ?, ?, ?)",
                         (f"m{self.seq}", session_id, tipo, self.seq, json.dumps(dados)))
        self.con.commit()

    def vivas(self):
        return {s.session_id: s for s in self.provedor.sessoes_vivas()}

    def test_sessao_em_andamento(self):
        self.sessao("ses_a", directory="/home/d/.local/share/opencode/worktree/x/feat")
        self.mensagem("ses_a", "user", "primeira")
        self.mensagem("ses_a", "user", "  segunda\n  pergunta ")
        self.mensagem("ses_a", "assistant")

        s = self.vivas()["ses_a"]

        self.assertEqual(("opencode", 0, None, "/home/d/repo",
                          "/home/d/.local/share/opencode/worktree/x/feat", "Minha sessão",
                          "segunda pergunta", "2.0.29", AGORA - 3600, "busy"),
                         (s.provedor, s.pid, s.tty, s.dir_resume, s.dir_trabalho, s.titulo,
                          s.ultima_msg, s.versao, s.ultima_atividade, s.status))
        self.assertEqual(["opencode", "-s", "ses_a"], self.provedor.comando_resume("ses_a"))

    def test_fim_de_turno_e_parada_e_sem_mensagem_nao_tem_status(self):
        self.sessao("ses_parada")
        self.mensagem("ses_parada", "user", "oi")
        self.mensagem("ses_parada", "assistant")
        self.mensagem("ses_parada", "idle")
        self.sessao("ses_vazia")

        vivas = self.vivas()

        self.assertEqual("idle", vivas["ses_parada"].status)
        self.assertIsNone(vivas["ses_vazia"].status)
        self.assertIsNone(vivas["ses_vazia"].ultima_msg)

    def test_mais_de_10_dias_sem_atividade_nao_esta_aberta(self):
        self.sessao("ses_recente", atividade=AGORA - 10 * DIA + 60)
        self.sessao("ses_velha", atividade=AGORA - 10 * DIA - 60)

        self.assertEqual({"ses_recente"}, set(self.vivas()))

    def test_prazo_vem_do_ambiente(self):
        self.sessao("ses_a", atividade=AGORA - 3 * DIA)
        with unittest.mock.patch.dict("os.environ", {"LLM_SESSOES_OPENCODE_DIAS": "2"}):
            provedor = ProvedorOpencode(raiz=self.raiz, relogio=lambda: AGORA)

        self.assertEqual([], provedor.sessoes_vivas())

    def test_ignora_arquivada_e_subsessao(self):
        self.sessao("ses_pai")
        self.sessao("ses_arquivada", time_archived=AGORA * 1000)
        self.sessao("ses_filha", parent_id="ses_pai")

        self.assertEqual({"ses_pai"}, set(self.vivas()))

    def test_projeto_global_retoma_no_proprio_diretorio(self):
        self.sessao("ses_g", projeto="global", directory="/tmp/x", title="  título\n quebrado ")

        s = self.vivas()["ses_g"]

        self.assertEqual(("/tmp/x", "título quebrado"), (s.dir_resume, s.titulo))

    def test_branch_lida_do_git_inclusive_em_worktree(self):
        repo = Path(self.tmp.name) / "repo"
        (repo / ".git" / "worktrees" / "wt").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (repo / ".git" / "worktrees" / "wt" / "HEAD").write_text("ref: refs/heads/feat/x\n")
        wt = Path(self.tmp.name) / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {repo}/.git/worktrees/wt\n")
        destacado = Path(self.tmp.name) / "destacado"
        (destacado / ".git").mkdir(parents=True)
        (destacado / ".git" / "HEAD").write_text("0123abcd\n")
        self.sessao("ses_raiz", directory=str(repo))
        self.sessao("ses_wt", directory=str(wt))
        self.sessao("ses_head", directory=str(destacado))
        self.sessao("ses_sem_git", directory="/nao/existe")

        vivas = self.vivas()

        self.assertEqual(("main", "feat/x", None, None),
                         tuple(vivas[i].branch for i in ("ses_raiz", "ses_wt", "ses_head",
                                                        "ses_sem_git")))

    def test_sem_opencode_instalado_nao_ha_sessoes_nem_aviso(self):
        provedor = ProvedorOpencode(raiz=Path(self.tmp.name) / "nada", relogio=lambda: AGORA)

        self.assertEqual([], provedor.sessoes_vivas())
        self.assertEqual([], provedor.avisos)

    def test_esquema_que_mudou_vira_aviso(self):
        self.con.execute("ALTER TABLE session_v2 RENAME TO session_v3")
        self.con.commit()

        self.assertEqual([], self.provedor.sessoes_vivas())
        self.assertEqual(1, len(self.provedor.avisos))
        self.assertIn("esquema mudou?", self.provedor.avisos[0])


if __name__ == "__main__":
    unittest.main()
