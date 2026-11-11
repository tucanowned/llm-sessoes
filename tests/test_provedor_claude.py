import json
import os
import tempfile
import unittest
from pathlib import Path

from llm_sessoes.provedores.claude import ProvedorClaude

ID_A = "aaaaaaaa-0000-4000-8000-000000000001"
ID_B = "bbbbbbbb-0000-4000-8000-000000000002"
TTY_PTS_4 = 136 * 256 + 4


class ProvedorClaudeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.raiz = base / "claude"
        self.proc = base / "proc"
        (self.raiz / "sessions").mkdir(parents=True)
        (self.raiz / "projects").mkdir()
        self.proc.mkdir()
        self.provedor = ProvedorClaude(raiz=self.raiz, proc=self.proc)

    def tearDown(self):
        self.tmp.cleanup()

    # --- montagem do cenário ------------------------------------------------------

    def processo(self, pid, starttime="500", comm="claude", cmdline=("claude",),
                 cwd="/repo", tty_nr=TTY_PTS_4):
        d = self.proc / str(pid)
        d.mkdir()
        campos = ["S", "1", "1", "1", str(tty_nr)] + ["0"] * 14 + [starttime] + ["0"] * 5
        (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(campos))
        (d / "cmdline").write_bytes(b"\0".join(c.encode() for c in cmdline) + b"\0")
        os.symlink(cwd, d / "cwd")

    def registro(self, pid, session_id, proc_start="500", kind="interactive",
                 cwd="/repo", status="idle"):
        (self.raiz / "sessions" / f"{pid}.json").write_text(json.dumps({
            "pid": pid, "sessionId": session_id, "cwd": cwd, "procStart": proc_start,
            "kind": kind, "status": status,
        }))

    def conversa(self, session_id, linhas, projeto="-repo"):
        d = self.raiz / "projects" / projeto
        d.mkdir(exist_ok=True)
        (d / f"{session_id}.jsonl").write_text(
            "\n".join(l if isinstance(l, str) else json.dumps(l) for l in linhas) + "\n")

    # --- testes -------------------------------------------------------------------

    def test_sessao_interativa_viva(self):
        self.processo(10)
        self.registro(10, ID_A, status="busy")

        [s] = self.provedor.sessoes_vivas()

        self.assertEqual(("claude", ID_A, 10, "/repo", "pts/4", "busy"),
                         (s.provedor, s.session_id, s.pid, s.dir_resume, s.tty, s.status))

    def test_ignora_arquivo_orfao_de_crash(self):
        self.registro(10, ID_A)  # sem processo correspondente

        self.assertEqual([], self.provedor.sessoes_vivas())

    def test_ignora_pid_reutilizado_por_outro_processo(self):
        self.processo(10, starttime="999", comm="bash")
        self.registro(10, ID_A, proc_start="500")

        self.assertEqual([], self.provedor.sessoes_vivas())

    def test_ignora_processos_em_background(self):
        self.processo(10, cmdline=("claude", "--resume", ID_A))
        self.registro(10, ID_A, kind="bg")

        self.assertEqual([], self.provedor.sessoes_vivas())

    def test_json_corrompido_nao_derruba_a_coleta(self):
        (self.raiz / "sessions" / "9.json").write_text("{corrompido")
        self.processo(10)
        self.registro(10, ID_A)

        self.assertEqual([ID_A], [s.session_id for s in self.provedor.sessoes_vivas()])

    def test_metadados_vem_do_fim_da_conversa(self):
        self.processo(10)
        self.registro(10, ID_A)
        self.conversa(ID_A, [
            {"type": "ai-title", "aiTitle": "Título antigo"},
            {"type": "user", "cwd": "/repo", "gitBranch": "main"},
            {"type": "ai-title", "aiTitle": "Título novo"},
            {"type": "user", "cwd": "/repo/.claude/worktrees/x", "gitBranch": "feat/x"},
            {"type": "last-prompt", "lastPrompt": "pode\n  seguir"},
        ])

        [s] = self.provedor.sessoes_vivas()

        self.assertEqual(("Título novo", "/repo/.claude/worktrees/x", "feat/x", "pode seguir"),
                         (s.titulo, s.dir_trabalho, s.branch, s.ultima_msg))
        self.assertEqual("/repo", s.dir_resume)

    def test_titulo_customizado_prevalece_sobre_o_da_ia(self):
        self.processo(10)
        self.registro(10, ID_A)
        self.conversa(ID_A, [
            {"type": "custom-title", "customTitle": "Meu nome"},
            {"type": "ai-title", "aiTitle": "Nome da IA"},
        ])

        self.assertEqual("Meu nome", self.provedor.sessoes_vivas()[0].titulo)

    def test_amplia_a_cauda_quando_o_titulo_esta_longe_do_fim(self):
        self.processo(10)
        self.registro(10, ID_A)
        enchimento = {"type": "assistant", "texto": "x" * 1024}
        self.conversa(ID_A, [{"type": "ai-title", "aiTitle": "Lá no começo"}] + [enchimento] * 400)

        self.assertEqual("Lá no começo", self.provedor.sessoes_vivas()[0].titulo)

    def test_linha_cortada_ou_invalida_e_ignorada(self):
        self.processo(10)
        self.registro(10, ID_A)
        self.conversa(ID_A, ['{"type":"ai-ti', "[1,2]", {"type": "ai-title", "aiTitle": "Ok"}])

        self.assertEqual("Ok", self.provedor.sessoes_vivas()[0].titulo)

    def test_sessao_sem_conversa_gravada_vem_sem_metadados(self):
        self.processo(10)
        self.registro(10, ID_A)

        [s] = self.provedor.sessoes_vivas()

        self.assertIsNone(s.titulo)
        self.assertIsNone(s.dir_trabalho)

    def test_fallback_usa_resume_da_linha_de_comando(self):
        self.processo(10)
        self.registro(10, ID_A)
        self.processo(20, cmdline=("claude", "--resume", ID_B), cwd="/outro")  # sem registro

        vivas = {s.session_id: s for s in self.provedor.sessoes_vivas()}

        self.assertEqual({ID_A, ID_B}, set(vivas))
        self.assertEqual("/outro", vivas[ID_B].dir_resume)

    def test_fallback_ignora_processo_sem_terminal_ou_sem_id(self):
        self.processo(20, cmdline=("claude", "--resume", ID_B), tty_nr=0)
        self.processo(30, cmdline=("claude",))

        self.assertEqual([], self.provedor.sessoes_vivas())

    def test_processo_interativo_sem_registro_gera_aviso(self):
        self.processo(10)
        self.registro(10, ID_A)
        self.processo(20, cmdline=("claude", "--resume", ID_B))
        self.processo(30, cmdline=("claude", "-p", "x"), tty_nr=0)  # sem terminal: não conta

        self.provedor.sessoes_vivas()

        self.assertEqual(1, len(self.provedor.avisos))
        self.assertIn("pid=20", self.provedor.avisos[0])

    def test_coleta_saudavel_nao_gera_aviso_e_traz_a_versao(self):
        self.processo(10)
        self.registro(10, ID_A)
        sessoes = json.loads((self.raiz / "sessions/10.json").read_text())
        (self.raiz / "sessions/10.json").write_text(json.dumps({**sessoes, "version": "2.1.274"}))

        [s] = self.provedor.sessoes_vivas()

        self.assertEqual(([], "2.1.274"), (self.provedor.avisos, s.versao))

    def test_avisos_sao_zerados_a_cada_coleta(self):
        self.processo(20, cmdline=("claude",))
        self.provedor.sessoes_vivas()
        self.assertEqual(1, len(self.provedor.avisos))
        (self.proc / "20" / "stat").unlink()  # processo morreu

        self.provedor.sessoes_vivas()

        self.assertEqual([], self.provedor.avisos)


if __name__ == "__main__":
    unittest.main()
