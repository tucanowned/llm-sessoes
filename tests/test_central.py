import http.client
import json
import threading
import unittest
from unittest import mock

from llm_sessoes.central import pagina
from llm_sessoes.central.armazem import (OFFLINE_APOS, RETENCAO_EVENTOS, Armazem,
                                         RelatorioInvalido, online, validar)
from llm_sessoes.central.servidor import LIMITE_CORPO, Handler, Servidor

AGORA = 1_800_000_000


def sessao(id_, **extra):
    return {"provedor": "claude", "id": id_, "titulo": f"título {id_}", "ultima_msg": None,
            "dir_resume": "/home/d/repo", "dir_trabalho": None, "branch": "main",
            "status": "trabalhando", "inicio": AGORA - 600, "ultima_atividade": AGORA - 60,
            "tmux": None, **extra}


def relatorio(maquina="pc", vivas=(), eventos=(), anomalias=()):
    return {"versao_esquema": 1, "maquina": maquina, "boot_id": "boot", "coletado_em": AGORA,
            "versao_cliente": "0.2.0", "vivas": list(vivas), "eventos": list(eventos),
            "anomalias": list(anomalias)}


class ArmazemTest(unittest.TestCase):
    def setUp(self):
        self.a = Armazem(":memory:")

    def tearDown(self):
        self.a.fechar()

    def gravar(self, quando=AGORA, **kw):
        self.a.gravar(validar(relatorio(**kw)), quando)

    def test_novo_retrato_substitui_as_vivas_da_maquina(self):
        self.gravar(vivas=[sessao("a"), sessao("b")])
        self.gravar(maquina="outra", vivas=[sessao("x")])

        self.gravar(vivas=[sessao("b", titulo="renomeada")])

        pc, outra = sorted(self.a.painel(AGORA), key=lambda m: m["nome"] != "pc")
        self.assertEqual([("b", "renomeada")], [(s["session_id"], s["titulo"]) for s in pc["vivas"]])
        self.assertEqual(["x"], [s["session_id"] for s in outra["vivas"]])

    def test_eventos_acumulam_sem_duplicar_e_expiram_em_30_dias(self):
        antigo = {**sessao("v"), "tipo": "fechou", "quando": AGORA - RETENCAO_EVENTOS - 10}
        recente = {**sessao("a"), "tipo": "fechou", "quando": AGORA - 100}
        self.gravar(quando=AGORA - 3600, eventos=[antigo])
        self.gravar(eventos=[recente])
        self.gravar(eventos=[recente])  # reenvio do mesmo retrato

        total = self.a.con.execute("SELECT session_id FROM eventos").fetchall()
        self.assertEqual(["a"], [l[0] for l in total])

    def test_recentes_mostra_fechadas_e_crash_sem_as_reabertas(self):
        self.gravar(vivas=[sessao("reaberta")], eventos=[
            {**sessao("f"), "tipo": "fechou", "quando": AGORA - 100},
            {**sessao("c"), "tipo": "crash", "quando": AGORA - 200},
            {**sessao("reaberta"), "tipo": "fechou", "quando": AGORA - 300},
            {**sessao("velha"), "tipo": "fechou", "quando": AGORA - 49 * 3600},
            {**sessao("f"), "tipo": "abriu", "quando": AGORA - 900},
        ])

        [pc] = self.a.painel(AGORA)

        self.assertEqual([("f", "fechou"), ("c", "crash")],
                         [(s["session_id"], s["tipo"]) for s in pc["recentes"]])

    def test_online_ate_5_minutos_sem_relatorio(self):
        self.assertTrue(online(AGORA - OFFLINE_APOS, AGORA))
        self.assertFalse(online(AGORA - OFFLINE_APOS - 1, AGORA))
        self.assertFalse(online(None, AGORA))

    def test_painel_poe_online_primeiro(self):
        self.gravar(maquina="a-velha", quando=AGORA - 3600)
        self.gravar(maquina="b-nova")

        self.assertEqual([("b-nova", True), ("a-velha", False)],
                         [(m["nome"], m["online"]) for m in self.a.painel(AGORA)])

    def test_resumo(self):
        self.gravar(maquina="on", vivas=[sessao("a"), sessao("b")], eventos=[
            {**sessao("c"), "tipo": "crash", "quando": AGORA - 3600},
            {**sessao("c"), "tipo": "crash", "quando": AGORA - 3600},
            {**sessao("d"), "tipo": "crash", "quando": AGORA - 25 * 3600},
        ])
        self.gravar(maquina="off", quando=AGORA - 600, vivas=[sessao("z")],
                    eventos=[{**sessao("e"), "tipo": "crash", "quando": AGORA - 60}])

        self.assertEqual({"ativas": 2, "maquinas_online": 1, "maquinas_total": 2,
                          "crashes_24h": 2}, self.a.resumo(AGORA))


class ValidarTest(unittest.TestCase):
    def test_rejeita_formato_errado(self):
        casos = [
            [],
            {**relatorio(), "versao_esquema": 2},
            relatorio(maquina="pc; rm -rf /"),
            relatorio(maquina=""),
            {**relatorio(), "vivas": {}},
            relatorio(vivas=[{"provedor": "claude"}]),
            relatorio(eventos=[{**sessao("a"), "tipo": "explodiu", "quando": 1}]),
            relatorio(eventos=[{**sessao("a"), "tipo": "fechou", "quando": "ontem"}]),
            relatorio(vivas=[sessao("a", inicio=float("nan"))]),
        ]
        for caso in casos:
            with self.subTest(caso=caso), self.assertRaises(RelatorioInvalido):
                validar(caso)

    def test_trunca_textos_e_descarta_tmux_malformado(self):
        r = validar(relatorio(vivas=[sessao("a", titulo="t" * 5000, tmux={"sessao": 3})]))

        self.assertEqual(500, len(r["vivas"][0]["titulo"]))
        self.assertIsNone(r["vivas"][0]["tmux"])


class ServidorTest(unittest.TestCase):
    def setUp(self):
        silencio = mock.patch.object(Handler, "log_message")
        silencio.start()
        self.addCleanup(silencio.stop)
        self.armazem = Armazem(":memory:")
        self.servidor = Servidor(("127.0.0.1", 0), self.armazem, ["segredo", "outro"],
                                 relogio=lambda: AGORA)
        self.thread = threading.Thread(target=self.servidor.serve_forever, args=(0.05,),
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.servidor.shutdown()
        self.servidor.server_close()
        self.armazem.fechar()

    def pedir(self, metodo, caminho, corpo=None, headers=None):
        con = http.client.HTTPConnection(*self.servidor.server_address, timeout=5)
        try:
            con.request(metodo, caminho, body=corpo, headers=headers or {})
            resposta = con.getresponse()
            return resposta.status, dict(resposta.getheaders()), resposta.read().decode()
        finally:
            con.close()

    def postar(self, dado, token="segredo"):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        corpo = dado if isinstance(dado, bytes) else json.dumps(dado).encode()
        return self.pedir("POST", "/api/v1/relatorio", corpo, headers)

    def test_healthz(self):
        self.assertEqual(200, self.pedir("GET", "/healthz")[0])

    def test_relatorio_autenticado_e_gravado(self):
        status, _, corpo = self.postar(relatorio(vivas=[sessao("a")]), token="outro")

        self.assertEqual(200, status, corpo)
        self.assertEqual(1, self.armazem.resumo(AGORA)["ativas"])

    def test_sem_token_ou_token_errado_da_401(self):
        for token in (None, "errado", ""):
            with self.subTest(token=token):
                status, headers, _ = self.postar(relatorio(), token=token)
                self.assertEqual(401, status)
                self.assertEqual("Bearer", headers["WWW-Authenticate"])
        self.assertEqual(0, self.armazem.resumo(AGORA)["maquinas_total"])

    def test_corpo_acima_de_1mb_da_413_sem_ler(self):
        # Só os headers: o servidor tem que recusar pelo Content-Length, antes de ler o corpo.
        con = http.client.HTTPConnection(*self.servidor.server_address, timeout=5)
        con.putrequest("POST", "/api/v1/relatorio")
        con.putheader("Authorization", "Bearer segredo")
        con.putheader("Content-Length", str(LIMITE_CORPO + 1))
        con.endheaders()
        status = con.getresponse().status
        con.close()

        self.assertEqual(413, status)
        self.assertEqual(0, self.armazem.resumo(AGORA)["maquinas_total"])

    def test_corpo_de_quase_1mb_ainda_passa(self):
        r = relatorio(anomalias=["x" * 1000])
        r["anomalias"] += ["y"] * ((LIMITE_CORPO - len(json.dumps(r))) // 5 - 10)
        corpo = json.dumps(r).encode()
        self.assertLess(LIMITE_CORPO - 100, len(corpo))

        self.assertEqual(200, self.postar(corpo)[0])

    def test_json_invalido_e_esquema_invalido_dao_400(self):
        self.assertEqual(400, self.postar(b"{nao e json")[0])
        status, _, corpo = self.postar({**relatorio(), "versao_esquema": 9})
        self.assertEqual(400, status)
        self.assertIn("versao_esquema", json.loads(corpo)["erro"])

    def test_resumo_para_o_homepage(self):
        self.postar(relatorio(vivas=[sessao("a")]))

        status, headers, corpo = self.pedir("GET", "/api/v1/resumo")

        self.assertEqual(200, status)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertEqual({"ativas": 1, "maquinas_online": 1, "maquinas_total": 1,
                          "crashes_24h": 0}, json.loads(corpo))

    def test_pagina_escapa_o_conteudo_das_sessoes(self):
        malicioso = '<script>alert("x")</script>'
        self.postar(relatorio(
            vivas=[sessao("a", titulo=malicioso, ultima_msg="<img src=x onerror=alert(1)>",
                          branch='"><u>sublinhado', tmux={"sessao": "s'; rm", "janela": 1, "painel": "%1"})],
            eventos=[{**sessao("f", titulo=malicioso, dir_resume="/x'\"<y>"),
                      "tipo": "crash", "quando": AGORA - 60}],
            anomalias=["<marquee>anomalia</marquee>"]))

        status, headers, html = self.pedir("GET", "/")

        self.assertEqual(200, status)
        self.assertNotIn("<script>alert", html)
        self.assertNotIn("<img src=x", html)
        self.assertNotIn("<marquee>", html)
        self.assertNotIn("<u>", html)
        self.assertIn("&quot;&gt;&lt;u&gt;sublinhado", html)
        self.assertIn("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;", html)
        self.assertIn("&lt;marquee&gt;anomalia", html)
        self.assertIn("script-src 'nonce-", headers["Content-Security-Policy"])
        self.assertEqual(1, html.count("<script"))  # só o script do próprio painel

    def test_rota_desconhecida(self):
        self.assertEqual(404, self.pedir("GET", "/nada")[0])
        self.assertEqual(404, self.pedir("POST", "/api/v1/outra", b"{}",
                                         {"Authorization": "Bearer segredo"})[0])


class PaginaTest(unittest.TestCase):
    def test_comando_de_retomada_remota(self):
        s = {"provedor": "claude", "session_id": "abc", "dir_resume": "/home/d/meu repo"}

        self.assertEqual("ssh pc -t 'cd '\"'\"'/home/d/meu repo'\"'\"' && claude --resume abc'",
                         pagina.comando_retomar("pc", s))
        self.assertEqual("ssh pc -t 'cd /r && codex resume t1'", pagina.comando_retomar(
            "pc", {"provedor": "codex", "session_id": "t1", "dir_resume": "/r"}))

    def test_sessao_no_tmux_copia_o_attach(self):
        s = {"provedor": "claude", "session_id": "abc", "dir_resume": "/r",
             "tmux": {"sessao": "dev", "janela": 2, "painel": "%3"}}

        self.assertEqual("ssh pc -t tmux attach -t dev", pagina.comando_retomar("pc", s))

    def test_provedor_desconhecido_fica_sem_botao(self):
        self.assertIsNone(pagina.comando_retomar("pc", {"provedor": "x", "session_id": "a",
                                                        "dir_resume": "/r"}))

    def test_pagina_mostra_online_offline_e_anomalias(self):
        a = Armazem(":memory:")
        a.gravar(validar(relatorio(maquina="on", vivas=[sessao("a", tmux={
            "sessao": "dev", "janela": 2, "painel": "%3"})], anomalias=["codex: sem lock"])), AGORA)
        a.gravar(validar(relatorio(maquina="off")), AGORA - 3600)

        html = pagina.renderizar(a.painel(AGORA), a.resumo(AGORA), AGORA, "n")
        a.fechar()

        self.assertIn('class="maquina online"', html)
        self.assertIn('class="maquina offline"', html)
        self.assertIn("último envio há 1 h", html)
        self.assertIn("codex: sem lock", html)
        self.assertIn("tmux <code>dev:2</code>", html)
        self.assertIn('data-cmd="ssh on -t tmux attach -t dev"', html)
        self.assertIn('http-equiv="refresh" content="30"', html)


if __name__ == "__main__":
    unittest.main()
