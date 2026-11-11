import tempfile
import time
import unittest
from pathlib import Path

from llm_sessoes import reabrir
from llm_sessoes.banco import Banco
from llm_sessoes.coletor import coletar
from llm_sessoes.modelos import SessaoViva

try:
    from textual.widgets import DataTable, Input

    from llm_sessoes.tui.app import SessoesApp
except ModuleNotFoundError:  # python do sistema, sem o .venv da TUI
    SessoesApp = None


class ProvedorFalso:
    nome = "claude"

    def __init__(self, sessoes):
        self.sessoes = sessoes

    def sessoes_vivas(self):
        return self.sessoes


@unittest.skipUnless(SessoesApp, "Textual não instalado (rode com .venv/bin/python)")
class SessoesAppTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.caminho = Path(self.tmp.name) / "sessoes.db"
        self.agora = int(time.time())
        self.pedidos = []

    def sessao(self, session_id, titulo, pid):
        return SessaoViva("claude", session_id, pid, dir_resume=self.tmp.name, titulo=titulo)

    def coletar(self, sessoes, quando, boot="boot-1"):
        banco = Banco(self.caminho)
        coletar(banco, [ProvedorFalso(sessoes)], quando, boot)
        banco.fechar()

    def app(self):
        def reabrir_falso(sessoes):
            self.pedidos.append([s["session_id"] for s in sessoes])
            return reabrir.Resultado(list(sessoes), [])
        return SessoesApp(self.caminho, reabrir_sessoes=reabrir_falso)

    def tres_fechadas(self):
        sessoes = [self.sessao("id-a", "Agenda TelosChat", 10), self.sessao("id-b", "Migração R2", 20),
                   self.sessao("id-c", "Perf dashboard", 30)]
        self.coletar(sessoes, self.agora - 600)
        self.coletar([], self.agora - 60)

    async def test_abre_na_aba_de_crash_quando_ha_pendentes(self):
        a, b = self.sessao("id-a", "Voltou", 10), self.sessao("id-b", "Ficou", 20)
        self.coletar([a, b], self.agora - 600, boot="boot-1")
        self.coletar([a], self.agora, boot="boot-2")

        async with self.app().run_test() as pilot:
            self.assertEqual("crash", pilot.app.aba)
            self.assertEqual(["id-b", "id-a"], [i.session_id for i in pilot.app.visiveis])

    async def test_enter_reabre_a_linha_do_cursor(self):
        self.tres_fechadas()

        async with self.app().run_test() as pilot:
            await pilot.press("down", "enter")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            self.assertEqual("fechadas", pilot.app.aba)
            self.assertEqual(1, len(self.pedidos))
            self.assertEqual(1, len(self.pedidos[0]))
            self.assertEqual(1, len(pilot.app.reabertas_agora))

    async def test_enter_reabre_as_marcadas(self):
        self.tres_fechadas()

        async with self.app().run_test() as pilot:
            await pilot.press("space", "space", "enter")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            self.assertEqual(2, len(self.pedidos[0]))
            self.assertEqual(set(), pilot.app.marcadas)

    async def test_nao_reabre_a_mesma_sessao_duas_vezes(self):
        self.tres_fechadas()

        async with self.app().run_test() as pilot:
            await pilot.press("enter")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.app.workers.wait_for_complete()

            self.assertEqual(1, len(self.pedidos))

    async def test_busca_filtra_e_esc_limpa(self):
        self.tres_fechadas()

        async with self.app().run_test() as pilot:
            await pilot.press("slash", *"migracao")
            self.assertEqual(["id-b"], [i.session_id for i in pilot.app.visiveis])
            self.assertEqual(1, pilot.app.query_one(DataTable).row_count)

            await pilot.press("enter", "enter")  # 1º sai da busca; 2º reabre o que sobrou
            await pilot.app.workers.wait_for_complete()
            self.assertEqual([["id-b"]], self.pedidos)

            await pilot.press("escape")
            self.assertEqual("", pilot.app.query_one(Input).value)
            self.assertEqual(3, len(pilot.app.visiveis))

    async def test_sessao_aberta_nao_e_reaberta(self):
        self.coletar([self.sessao("id-a", "Aberta", 10)], self.agora)

        async with self.app().run_test() as pilot:
            self.assertEqual("abertas", pilot.app.aba)
            await pilot.press("enter")
            await pilot.app.workers.wait_for_complete()

            self.assertEqual([], self.pedidos)

    async def test_trocar_de_aba_desmarca(self):
        self.tres_fechadas()

        async with self.app().run_test() as pilot:
            await pilot.press("a")
            self.assertEqual(3, len(pilot.app.marcadas))

            await pilot.press("4")

            self.assertEqual("todas", pilot.app.aba)
            self.assertEqual(set(), pilot.app.marcadas)
            self.assertEqual(3, len(pilot.app.visiveis))

    async def test_muitas_abas_pedem_confirmacao(self):
        sessoes = [self.sessao(f"id-{n}", f"Sessão {n}", n + 10) for n in range(7)]
        self.coletar(sessoes, self.agora - 600)
        self.coletar([], self.agora - 60)

        async with self.app().run_test() as pilot:
            await pilot.press("a", "enter")
            await pilot.pause()
            self.assertEqual([], self.pedidos)

            await pilot.press("s")
            await pilot.app.workers.wait_for_complete()
            self.assertEqual(7, len(self.pedidos[0]))


if __name__ == "__main__":
    unittest.main()
