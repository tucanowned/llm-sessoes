import tempfile
import unittest
from unittest import mock

from llm_sessoes import reabrir


def sessao(**campos):
    base = {"provedor": "claude", "session_id": "cccccccc-0000-4000-8000-000000000003",
            "titulo": "Perf dashboard", "dir_resume": "/repo"}
    return {**base, **campos}


class TituloDaAbaTest(unittest.TestCase):
    def test_prefixa_o_provedor(self):
        self.assertEqual("claude: Perf dashboard", reabrir.titulo_da_aba(sessao()))

    def test_tira_o_que_quebra_a_linha_de_comando_do_wt(self):
        titulo = reabrir.titulo_da_aba(sessao(titulo='corrigir "x"; depois\n  y'))

        self.assertEqual("claude: corrigir 'x', depois y", titulo)

    def test_corta_titulo_longo(self):
        titulo = reabrir.titulo_da_aba(sessao(titulo="x" * 200))

        self.assertEqual(len("claude: ") + reabrir.LIMITE_TITULO, len(titulo))
        self.assertTrue(titulo.endswith("…"))

    def test_sem_titulo_usa_o_nome_do_diretorio(self):
        titulo = reabrir.titulo_da_aba(sessao(titulo=None, dir_resume="/home/eu/meu-projeto"))

        self.assertEqual("claude: meu-projeto", titulo)


class ArgumentosWtTest(unittest.TestCase):
    def test_monta_new_tab_chamando_o_modo_aba_dentro_do_wsl(self):
        args = reabrir.argumentos_wt("/mnt/c/wt.exe", "Debian", "/repo/bin/llm-sessoes",
                                     sessao(provedor="codex", session_id="id-1", titulo="Migração"))

        self.assertEqual(
            ["/mnt/c/wt.exe", "-w", "0", "new-tab", "--title", "codex: Migração",
             "--suppressApplicationTitle", reabrir.WSL_EXE, "-d", "Debian", "--",
             "/repo/bin/llm-sessoes", "_aba", "codex", "id-1"],
            args)

    def test_diretorio_nao_atravessa_a_linha_de_comando(self):
        args = reabrir.argumentos_wt("wt.exe", "Debian", "/l", sessao(dir_resume="/repo; rm -rf x"))

        self.assertFalse(any(";" in a for a in args))


class ReabrirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.chamadas = []
        for alvo, valor in (("localizar_wt", "/mnt/c/wt.exe"), ("distro", "Debian"),
                            ("lancador", "/repo/bin/llm-sessoes")):
            p = mock.patch.object(reabrir, alvo, return_value=valor)
            p.start()
            self.addCleanup(p.stop)

    def test_abre_uma_aba_por_sessao(self):
        a = sessao(session_id="id-a", dir_resume=self.tmp.name)
        b = sessao(session_id="id-b", dir_resume=self.tmp.name)

        r = reabrir.reabrir([a, b], executar=self.chamadas.append, pausa=0)

        self.assertEqual([a, b], r.abertas)
        self.assertEqual(["id-a", "id-b"], [c[-1] for c in self.chamadas])

    def test_pula_sessao_cujo_diretorio_sumiu(self):
        boa = sessao(session_id="id-a", dir_resume=self.tmp.name)
        orfa = sessao(session_id="id-b", dir_resume=self.tmp.name + "/sumiu")

        r = reabrir.reabrir([orfa, boa], executar=self.chamadas.append, pausa=0)

        self.assertEqual([boa], r.abertas)
        self.assertEqual([orfa], [s for s, _ in r.puladas])
        self.assertIn("diretório não existe", r.puladas[0][1])
        self.assertEqual(1, len(self.chamadas))

    def test_simular_nao_executa_nada(self):
        r = reabrir.reabrir([sessao(dir_resume=self.tmp.name)], simular=True,
                            executar=self.chamadas.append)

        self.assertEqual(1, len(r.abertas))
        self.assertEqual([], self.chamadas)


class LocalizarWtTest(unittest.TestCase):
    def test_variavel_de_ambiente_tem_prioridade(self):
        with tempfile.NamedTemporaryFile() as falso, \
                mock.patch.dict("os.environ", {"LLM_SESSOES_WT": falso.name}):
            self.assertEqual(falso.name, reabrir.localizar_wt())

    def test_erro_claro_quando_nao_acha(self):
        with mock.patch.dict("os.environ", {"LLM_SESSOES_WT": ""}), \
                mock.patch.object(reabrir.shutil, "which", return_value=None), \
                mock.patch.object(reabrir.glob, "glob", return_value=[]):
            with self.assertRaisesRegex(reabrir.ErroReabrir, "LLM_SESSOES_WT"):
                reabrir.localizar_wt()


if __name__ == "__main__":
    unittest.main()
