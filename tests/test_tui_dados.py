import unittest

from llm_sessoes.banco import Banco
from llm_sessoes.coletor import coletar
from llm_sessoes.modelos import SessaoViva
from llm_sessoes.tui import dados


class ProvedorFalso:
    nome = "claude"

    def __init__(self, sessoes):
        self.sessoes = sessoes

    def sessoes_vivas(self):
        return self.sessoes


def sessao(session_id, titulo, pid, **campos):
    return SessaoViva("claude", session_id, pid, dir_resume="/code/meu-projeto", titulo=titulo, **campos)


AGORA = 1_800_000_000


class CarregarTest(unittest.TestCase):
    def setUp(self):
        self.banco = Banco(":memory:")
        self.addCleanup(self.banco.fechar)

    def coletar(self, sessoes, quando, boot="boot-1"):
        coletar(self.banco, [ProvedorFalso(sessoes)], quando, boot)

    def test_separa_abertas_de_fechadas(self):
        a, b = sessao("id-a", "Segue aberta", 10), sessao("id-b", "Fechei", 20)
        self.coletar([a, b], AGORA - 600)
        self.coletar([a], AGORA - 60)

        r = dados.carregar(self.banco, AGORA)

        self.assertEqual(["id-a"], [i.session_id for i in r.itens["abertas"]])
        self.assertEqual(["id-b"], [i.session_id for i in r.itens["fechadas"]])
        self.assertEqual(AGORA - 60, r.itens["fechadas"][0].quando)
        self.assertEqual(AGORA - 60, r.ultima_coleta)

    def test_fechadas_respeita_a_janela(self):
        self.coletar([sessao("id-a", "Antiga", 10)], AGORA - 50 * 3600)
        self.coletar([], AGORA - 49 * 3600)

        self.assertEqual([], dados.carregar(self.banco, AGORA, 24).itens["fechadas"])
        self.assertEqual(1, len(dados.carregar(self.banco, AGORA, 72).itens["fechadas"]))

    def test_crash_poe_pendentes_primeiro(self):
        a, b = sessao("id-a", "Voltou", 10), sessao("id-b", "Ficou para trás", 20)
        self.coletar([a, b], AGORA - 600, boot="boot-1")
        self.coletar([a], AGORA, boot="boot-2")

        crash = dados.carregar(self.banco, AGORA).itens["crash"]

        self.assertEqual([("id-b", dados.PENDENTE), ("id-a", dados.REABERTA)],
                         [(i.session_id, i.estado) for i in crash])
        self.assertFalse(crash[0].viva)
        self.assertTrue(crash[1].viva)

    def test_todas_traz_o_estado_de_cada_uma(self):
        a, b, c = sessao("id-a", "Aberta", 10), sessao("id-b", "Fechada", 20), sessao("id-c", "Caiu", 30)
        self.coletar([b, c], AGORA - 900, boot="boot-1")
        self.coletar([c], AGORA - 600, boot="boot-1")
        self.coletar([a], AGORA, boot="boot-2")

        todas = {i.session_id: i.estado for i in dados.carregar(self.banco, AGORA).itens["todas"]}

        self.assertEqual({"id-a": dados.ABERTA, "id-b": dados.FECHADA, "id-c": dados.CRASH}, todas)

    def test_worktree_e_quando_trabalha_fora_do_dir_de_resume(self):
        self.coletar([sessao("id-a", "Raiz", 10, dir_trabalho="/code/meu-projeto"),
                      sessao("id-b", "WT", 20, dir_trabalho="/code/meu-projeto/.claude/worktrees/nova-feature")],
                     AGORA)

        raiz, wt = dados.carregar(self.banco, AGORA).itens["abertas"]

        self.assertFalse(raiz.em_worktree)
        self.assertTrue(wt.em_worktree)
        self.assertEqual("nova-feature", wt.diretorio)
        self.assertEqual("/code/meu-projeto", wt.como_sessao()["dir_resume"])

    def test_sessao_iniciada_dentro_do_worktree_tambem_conta(self):
        dentro = SessaoViva("claude", "id-a", 10, dir_resume="/code/crm/.claude/worktrees/nova-feature")
        self.coletar([dentro], AGORA)

        self.assertTrue(dados.carregar(self.banco, AGORA).itens["abertas"][0].em_worktree)


class FiltrarTest(unittest.TestCase):
    def item(self, **campos):
        base = dict(provedor="claude", session_id="id", titulo="Migração do R2",
                    dir_resume="/code/plane-infra", dir_trabalho=None, branch="feat/backup",
                    ultima_msg=None, estado=dados.FECHADA, quando=0)
        return dados.Item(**{**base, **campos})

    def test_consulta_vazia_devolve_tudo(self):
        itens = [self.item(), self.item(session_id="outro")]

        self.assertEqual(itens, dados.filtrar(itens, "  "))

    def test_ignora_acento_e_maiusculas(self):
        self.assertEqual(1, len(dados.filtrar([self.item()], "migracao")))
        self.assertEqual(1, len(dados.filtrar([self.item()], "MIGRAÇÃO")))

    def test_busca_em_titulo_diretorio_branch_e_provedor(self):
        item = self.item(dir_trabalho="/code/plane-infra/.claude/worktrees/dns")
        for consulta in ("r2", "plane-infra", "worktrees/dns", "backup", "claude"):
            with self.subTest(consulta):
                self.assertEqual([item], dados.filtrar([item], consulta))

    def test_todos_os_termos_precisam_casar(self):
        self.assertEqual(1, len(dados.filtrar([self.item()], "r2 backup")))
        self.assertEqual([], dados.filtrar([self.item()], "r2 codex"))


class HaQuantoTest(unittest.TestCase):
    def test_escala_conforme_a_distancia(self):
        for segundos, esperado in ((30, "agora"), (300, "há 5 min"), (3 * 3600, "há 3 h"),
                                   (30 * 3600, "há 30 h"), (5 * 86400, "há 5 d")):
            with self.subTest(segundos):
                self.assertEqual(esperado, dados.ha_quanto(AGORA - segundos, AGORA))

    def test_relogio_atrasado_nao_vira_tempo_negativo(self):
        self.assertEqual("agora", dados.ha_quanto(AGORA + 50, AGORA))


if __name__ == "__main__":
    unittest.main()
