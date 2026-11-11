import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from llm_sessoes.provedores.codex import ProvedorCodex

ID_CONVERSA = "01a0af18-e730-7c11-8000-000000000001"
ID_AUXILIAR = "01a0af18-e7b1-7961-8000-000000000002"
TTY_PTS_14 = 136 * 256 + 14


class ProvedorCodexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.raiz = base / "codex"
        self.proc = base / "proc"
        (self.raiz / "sessions/2026/09/17").mkdir(parents=True)
        self.proc.mkdir()
        self.provedor = ProvedorCodex(raiz=self.raiz, proc=self.proc)

    def tearDown(self):
        self.tmp.cleanup()

    # --- montagem do cenário ------------------------------------------------------

    def processo(self, pid, abertos=(), comm="codex", cwd="/projeto", tty_nr=TTY_PTS_14):
        d = self.proc / str(pid)
        (d / "fd").mkdir(parents=True)
        campos = ["S", "1", "1", "1", str(tty_nr)] + ["0"] * 14 + ["500"] + ["0"] * 5
        (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(campos))
        (d / "cmdline").write_bytes(b"codex\0")
        os.symlink(cwd, d / "cwd")
        for n, destino in enumerate(abertos):
            os.symlink(str(destino), d / "fd" / str(n))

    def lock(self, thread_id):
        return self.raiz / "thread-writer-locks" / f"{thread_id}.lock"

    def rollout(self, thread_id, linhas=(), meta=None):
        arquivo = self.raiz / f"sessions/2026/09/17/rollout-2026-09-17T08-20-39-{thread_id}.jsonl"
        cabecalho = {"type": "session_meta", "payload": meta or {
            "id": thread_id, "cwd": "/projeto", "thread_source": "user"}}
        arquivo.write_text("\n".join(json.dumps(l) for l in [cabecalho, *linhas]) + "\n")
        return arquivo

    def estado(self, threads, versao=5):
        con = sqlite3.connect(self.raiz / f"state_{versao}.sqlite")
        con.execute("CREATE TABLE threads (id, rollout_path, cwd, title, name, git_branch, "
                    "first_user_message, thread_source, archived)")
        for t in threads:
            base = {"rollout_path": None, "cwd": "/projeto", "title": None, "name": None,
                    "git_branch": None, "first_user_message": None,
                    "thread_source": "user", "archived": 0}
            con.execute("INSERT INTO threads VALUES (:id, :rollout_path, :cwd, :title, :name, "
                        ":git_branch, :first_user_message, :thread_source, :archived)",
                        {**base, **t})
        con.commit()
        con.close()

    @staticmethod
    def msg_usuario(texto):
        return {"type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": texto}]}}

    # --- testes -------------------------------------------------------------------

    def test_conversa_identificada_pelo_lock_e_thread_auxiliar_ignorada(self):
        self.processo(10, abertos=[self.lock(ID_CONVERSA), self.lock(ID_AUXILIAR)])
        self.estado([{"id": ID_CONVERSA, "name": "Analise o projeto", "git_branch": "main",
                      "title": "Quero que analisa esse projeto…"}])

        [s] = self.provedor.sessoes_vivas()

        self.assertEqual(("codex", ID_CONVERSA, 10, "/projeto", "pts/14", "Analise o projeto", "main"),
                         (s.provedor, s.session_id, s.pid, s.dir_resume, s.tty, s.titulo, s.branch))
        self.assertEqual(["codex", "resume", ID_CONVERSA], self.provedor.comando_resume(ID_CONVERSA))

    def test_sem_nome_usa_o_titulo_truncado_em_uma_linha(self):
        self.processo(10, abertos=[self.lock(ID_CONVERSA)])
        self.estado([{"id": ID_CONVERSA, "title": "linha um\nlinha dois " + "x" * 200}])

        titulo = self.provedor.sessoes_vivas()[0].titulo

        self.assertTrue(titulo.startswith("linha um linha dois"))
        self.assertEqual(80, len(titulo))

    def test_ignora_subagente_e_arquivada(self):
        outro = "01a0213d-2cf3-7cc1-8000-000000000003"
        self.processo(10, abertos=[self.lock(ID_CONVERSA), self.lock(outro)])
        self.estado([{"id": ID_CONVERSA, "thread_source": "subagent"},
                     {"id": outro, "archived": 1}])

        self.assertEqual([], self.provedor.sessoes_vivas())

    def test_sessao_aberta_sem_nenhuma_mensagem_e_ignorada(self):
        self.processo(10, abertos=[self.lock(ID_CONVERSA), self.lock(ID_AUXILIAR)])
        self.estado([])

        self.assertEqual([], self.provedor.sessoes_vivas())

    def test_ignora_codex_sem_terminal_e_outros_processos(self):
        self.processo(10, abertos=[self.lock(ID_CONVERSA)], tty_nr=0)
        self.processo(20, abertos=[self.lock(ID_CONVERSA)], comm="node")
        self.estado([{"id": ID_CONVERSA}])

        self.assertEqual([], self.provedor.sessoes_vivas())

    def test_ultima_mensagem_pula_contexto_injetado(self):
        arquivo = self.rollout(ID_CONVERSA, [
            self.msg_usuario("<environment_context>…</environment_context>"),
            self.msg_usuario("primeira pergunta"),
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                  "content": [{"type": "output_text", "text": "ok"}]}},
            self.msg_usuario("segunda\n  pergunta"),
            self.msg_usuario("<turn_aborted/>"),
        ])
        self.processo(10, abertos=[self.lock(ID_CONVERSA), arquivo])
        self.estado([{"id": ID_CONVERSA}])

        self.assertEqual("segunda pergunta", self.provedor.sessoes_vivas()[0].ultima_msg)

    def test_rollout_vem_do_banco_quando_nao_esta_entre_os_abertos(self):
        arquivo = self.rollout(ID_CONVERSA, [self.msg_usuario("oi")])
        self.processo(10, abertos=[self.lock(ID_CONVERSA)])
        self.estado([{"id": ID_CONVERSA, "rollout_path": str(arquivo)}])

        self.assertEqual("oi", self.provedor.sessoes_vivas()[0].ultima_msg)

    def test_sem_banco_de_estado_cai_no_session_meta_do_rollout(self):
        arquivo = self.rollout(ID_CONVERSA, [self.msg_usuario("oi")], meta={
            "id": ID_CONVERSA, "cwd": "/projeto/sub", "thread_source": "user"})
        self.processo(10, abertos=[arquivo])

        [s] = self.provedor.sessoes_vivas()

        self.assertEqual((ID_CONVERSA, "/projeto/sub", None), (s.session_id, s.dir_trabalho, s.titulo))

    def test_esquema_incompativel_cai_no_rollout_em_vez_de_quebrar(self):
        arquivo = self.rollout(ID_CONVERSA)
        self.processo(10, abertos=[arquivo])
        con = sqlite3.connect(self.raiz / "state_6.sqlite")
        con.execute("CREATE TABLE outra_coisa (x)")
        con.close()

        self.assertEqual([ID_CONVERSA], [s.session_id for s in self.provedor.sessoes_vivas()])

    def test_usa_o_banco_de_estado_de_versao_mais_alta(self):
        self.processo(10, abertos=[self.lock(ID_CONVERSA)])
        self.estado([{"id": ID_CONVERSA, "name": "velho"}], versao=5)
        self.estado([{"id": ID_CONVERSA, "name": "novo"}], versao=12)

        self.assertEqual("novo", self.provedor.sessoes_vivas()[0].titulo)

    def test_codex_interativo_sem_nenhum_lock_gera_aviso(self):
        self.processo(10, abertos=["/home/x/.codex/state_5.sqlite"])
        self.estado([])

        self.assertEqual([], self.provedor.sessoes_vivas())
        self.assertIn("pid=10", self.provedor.avisos[0])

    def test_sessao_sem_mensagem_nao_e_anomalia(self):
        self.processo(10, abertos=[self.lock(ID_CONVERSA), self.lock(ID_AUXILIAR)])
        self.estado([])

        self.provedor.sessoes_vivas()

        self.assertEqual([], self.provedor.avisos)

    def test_esquema_incompativel_gera_um_unico_aviso(self):
        self.processo(10, abertos=[self.lock(ID_CONVERSA), self.lock(ID_AUXILIAR)])
        con = sqlite3.connect(self.raiz / "state_6.sqlite")
        con.execute("CREATE TABLE outra_coisa (x)")
        con.close()

        self.provedor.sessoes_vivas()

        self.assertEqual(1, len(self.provedor.avisos))
        self.assertIn("state_6.sqlite", self.provedor.avisos[0])


if __name__ == "__main__":
    unittest.main()
