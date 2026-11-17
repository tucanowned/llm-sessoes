import http.client
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from llm_sessoes.central import pagina
from llm_sessoes.central.pagina import ABAS
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
        html += self.pedir("GET", "/?aba=recentes")[2]

        self.assertEqual(200, status)
        self.assertNotIn("<script>alert", html)
        self.assertNotIn("<img src=x", html)
        self.assertNotIn("<marquee>", html)
        self.assertNotIn("<u>", html)
        self.assertIn("&quot;&gt;&lt;u&gt;sublinhado", html)
        self.assertIn("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;", html)
        self.assertIn("&lt;marquee&gt;anomalia", html)
        self.assertIn("script-src 'nonce-", headers["Content-Security-Policy"])
        self.assertEqual(2, html.count("<script"))  # só o script do painel, nas duas páginas

    def test_rota_desconhecida(self):
        self.assertEqual(404, self.pedir("GET", "/nada")[0])
        self.assertEqual(404, self.pedir("POST", "/api/v1/outra", b"{}",
                                         {"Authorization": "Bearer segredo"})[0])


class PaginaTest(unittest.TestCase):
    ID = "a78c1f02-0000-4000-8000-000000000001"

    def test_retomada_abre_tmux_novo_com_shell_interativo(self):
        s = {"provedor": "claude", "session_id": self.ID, "dir_resume": "/home/d/repo"}

        self.assertEqual(
            "ssh pc -t tmux new -A -s claude-a78c1f02 -c /home/d/repo "
            "''\"'\"'${SHELL:-zsh} -ic '\"'\"'\"'\"'\"'\"'\"'\"'claude --resume "
            f"{self.ID}'\"'\"'\"'\"'\"'\"'\"'\"''\"'\"''",
            pagina.comando_retomar("pc", s))

    def test_retomada_com_espaco_e_aspas_simples_no_diretorio(self):
        diretorio = "/home/d/meu repo/it's here"
        s = {"provedor": "codex", "session_id": self.ID, "dir_resume": diretorio}

        comando = pagina.comando_retomar("pc", s)

        # Camada 1, o shell local: separa os argumentos do ssh.
        local = shlex.split(comando)
        self.assertEqual(["ssh", "pc", "-t"], local[:3])
        # Camada 2: o ssh junta o resto com espaço e o shell remoto separa de novo.
        remoto = shlex.split(" ".join(local[3:]))
        self.assertEqual(["tmux", "new", "-A", "-s", "codex-a78c1f02", "-c", diretorio,
                          f"${{SHELL:-zsh}} -ic 'codex resume {self.ID}'"], remoto)
        # Camada 3: o tmux entrega a string ao shell, que chama o shell interativo.
        self.assertEqual(["${SHELL:-zsh}", "-ic", f"codex resume {self.ID}"],
                         shlex.split(remoto[-1]))

    @unittest.skipUnless(shutil.which("sh"), "precisa de um sh")
    def test_retomada_passa_por_shells_de_verdade(self):
        """Cola o comando num sh real, com ssh, tmux e o shell do usuário trocados por scripts
        que fazem o mesmo com os argumentos: o ssh junta tudo depois do `-t` com espaço e
        entrega a um shell novo; o tmux roda a string do comando com o shell padrão."""
        diretorio = "/tmp/meu repo/it's \"aqui\" $HOME"
        comando = pagina.comando_retomar("pc", {"provedor": "claude", "session_id": self.ID,
                                                "dir_resume": diretorio})
        with tempfile.TemporaryDirectory() as tmp:
            bin_ = Path(tmp)
            scripts = {
                "ssh": 'shift 2; exec sh -c "$*"',
                "tmux": 'printf "dir=%s\\n" "$6"; exec sh -c "$7"',
                "zsh-falso": 'printf "argv=%s|%s\\n" "$1" "$2"',
            }
            for nome, corpo in scripts.items():
                (bin_ / nome).write_text(f"#!/bin/sh\n{corpo}\n")
                (bin_ / nome).chmod(0o755)
            saida = subprocess.run(
                ["sh", "-c", comando], capture_output=True, text=True, timeout=10,
                env={"PATH": f"{bin_}:{os.environ['PATH']}", "SHELL": str(bin_ / "zsh-falso"),
                     "HOME": "/nao-expandir"}).stdout

        self.assertEqual(f"dir={diretorio}\nargv=-ic|claude --resume {self.ID}\n", saida)

    def test_sessao_no_tmux_copia_o_attach(self):
        s = {"provedor": "claude", "session_id": "abc", "dir_resume": "/r",
             "tmux": {"sessao": "dev", "janela": 2, "painel": "%3"}}

        self.assertEqual("ssh pc -t tmux attach -t dev", pagina.comando_retomar("pc", s))
        s["tmux"]["sessao"] = "meu projeto"
        self.assertEqual(["tmux", "attach", "-t", "meu projeto"],
                         shlex.split(" ".join(shlex.split(pagina.comando_retomar("pc", s))[3:])))

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
        self.assertIn('class="maquina compacta offline"', html)
        self.assertIn("último envio há 1 h", html)
        self.assertIn("codex: sem lock", html)
        self.assertIn("tmux <code>dev:2</code>", html)
        self.assertIn('data-cmd="ssh on -t tmux attach -t dev"', html)
        self.assertIn('http-equiv="refresh" content="30; url=?aba=ativas"', html)


class AbasTest(unittest.TestCase):
    """Abas globais Ativas / Fechadas e crash, escolhidas por ?aba= (sem JavaScript)."""

    def setUp(self):
        self.a = Armazem(":memory:")
        self.a.gravar(validar(relatorio(
            maquina="pc", anomalias=["codex: sem lock"],
            vivas=[sessao("viva-1", titulo="Sessão viva um"), sessao("viva-2", titulo="Sessão viva dois")],
            eventos=[{**sessao("f-velha", titulo="Fechada velha"), "tipo": "fechou", "quando": AGORA - 7200},
                     {**sessao("c-nova", titulo="Crash novo"), "tipo": "crash", "quando": AGORA - 60},
                     {**sessao("f-meio", titulo="Fechada do meio"), "tipo": "fechou", "quando": AGORA - 600}])),
            AGORA)
        self.a.gravar(validar(relatorio(maquina="nitro", vivas=[sessao("n-1", titulo="Viva do nitro")])),
                      AGORA)

    def tearDown(self):
        self.a.fechar()

    def html(self, aba=None):
        return pagina.renderizar(self.a.painel(AGORA), self.a.resumo(AGORA), AGORA, "n", aba)

    def test_aba_padrao_e_ativas(self):
        for aba in (None, "ativas"):
            with self.subTest(aba=aba):
                html = self.html(aba)
                self.assertIn('<a href="?aba=ativas" aria-current="page">', html)
                self.assertIn("Sessão viva um", html)

    def test_aba_ativas_mostra_so_as_vivas(self):
        html = self.html("ativas")

        self.assertIn("Sessão viva dois", html)
        self.assertIn("Viva do nitro", html)
        self.assertNotIn("Fechada velha", html)
        self.assertNotIn("Crash novo", html)

    def test_aba_recentes_mostra_so_fechadas_e_crash_da_mais_nova_para_a_mais_velha(self):
        html = self.html("recentes")

        self.assertNotIn("Sessão viva um", html)
        self.assertIn('<a href="?aba=recentes" aria-current="page" class="alerta">', html)
        posicoes = [html.index(t) for t in ("Crash novo", "Fechada do meio", "Fechada velha")]
        self.assertEqual(sorted(posicoes), posicoes)
        self.assertIn('<span class="evento crash">crash</span><span class="selo claude">claude'
                      '</span><span class="titulo">Crash novo', html)
        self.assertEqual(2, html.count('<span class="evento fechou">fechou</span>'))

    def test_maquina_sem_nada_na_aba_vira_linha_compacta(self):
        html = self.html("recentes")

        self.assertIn('<section class="maquina compacta online">', html)
        self.assertIn('<h2>nitro</h2><span class="sep">·</span>'
                      '<span class="nada-aqui">nada fechado nas últimas 48 h</span>', html)
        self.assertNotIn('<ul class="sessoes"></ul>', html)

    def test_status_e_anomalias_aparecem_nas_duas_abas(self):
        for aba in ABAS:
            with self.subTest(aba=aba):
                html = self.html(aba)
                self.assertIn("codex: sem lock", html)
                self.assertEqual(2, html.count('<span class="pilula online">online</span>'))

    def test_contadores_somam_todas_as_maquinas(self):
        html = self.html("ativas")

        self.assertIn('Ativas <span class="n">3</span>', html)
        self.assertIn('Fechadas e crash <span class="n">3</span>', html)

    def test_ativas_de_maquina_offline_nao_entram_no_contador_da_aba(self):
        self.a.gravar(validar(relatorio(maquina="desligada", vivas=[sessao("x"), sessao("y")])),
                      AGORA - 3600)

        html = self.html("ativas")

        self.assertIn('Ativas <span class="n">3</span>', html)  # 2 do pc + 1 do nitro
        self.assertIn("Máquina offline: sessões segundo o último envio.", html)

    def test_contagem_por_pc_no_cabecalho_com_link_para_a_outra_aba(self):
        ativas, recentes = self.html("ativas"), self.html("recentes")

        self.assertIn('<span class="contagem"><span>2 ativas</span> · <a href="?aba=recentes">'
                      '3 fechadas <span class="crash">(1 crash)</span></a></span>', ativas)
        self.assertIn('<span class="contagem"><a href="?aba=ativas">2 ativas</a> · '
                      '<span>3 fechadas <span class="crash">(1 crash)</span></span></span>', recentes)
        # nitro, compacto na aba de fechadas: só a contagem da outra aba
        self.assertIn('<span class="contagem"><a href="?aba=ativas">1 ativa</a></span>', recentes)
        # e na aba de ativas, o "0 fechadas" do nitro não é link
        self.assertIn('<span class="contagem"><span>1 ativa</span> · <span>0 fechadas</span></span>',
                      ativas)

    def test_contador_da_aba_de_crash(self):
        html = self.html("ativas")

        self.assertIn('Fechadas e crash <span class="n">3</span>'
                      ' <span class="crash24">● 1 crash em 24 h</span>', html)

    def test_sem_crash_em_24h_a_aba_nao_fica_vermelha(self):
        self.a.con.execute("UPDATE eventos SET quando = ? WHERE tipo = 'crash'", (AGORA - 30 * 3600,))

        html = self.html("ativas")

        self.assertIn('<a href="?aba=recentes">Fechadas e crash <span class="n">3</span></a>', html)
        self.assertNotIn("crash24", html.split("</style>")[1])

    def test_refresh_mantem_a_aba(self):
        self.assertIn('<meta http-equiv="refresh" content="30; url=?aba=recentes">',
                      self.html("recentes"))
        self.assertIn('<meta http-equiv="refresh" content="30; url=?aba=ativas">',
                      self.html("ativas"))

    def test_valor_invalido_cai_no_padrao_e_nao_e_reaproveitado(self):
        for valor in ("", "nada", '"><script>alert(1)</script>', "RECENTES"):
            with self.subTest(valor=valor):
                html = self.html(valor)
                self.assertIn('content="30; url=?aba=ativas"', html)
                self.assertIn('<a href="?aba=ativas" aria-current="page">', html)
                self.assertNotIn("alert(1)", html)


class AbasServidorTest(unittest.TestCase):
    """O ?aba= chega da URL de verdade, inclusive malformado."""

    def setUp(self):
        silencio = mock.patch.object(Handler, "log_message")
        silencio.start()
        self.addCleanup(silencio.stop)
        self.armazem = Armazem(":memory:")
        self.armazem.gravar(validar(relatorio(
            vivas=[sessao("v", titulo="sessao-zeta-viva")],
            eventos=[{**sessao("f", titulo="sessao-omega-fechada"), "tipo": "fechou", "quando": AGORA - 60}])),
            AGORA)
        self.servidor = Servidor(("127.0.0.1", 0), self.armazem, ["t"], relogio=lambda: AGORA)
        threading.Thread(target=self.servidor.serve_forever, args=(0.05,), daemon=True).start()

    def tearDown(self):
        self.servidor.shutdown()
        self.servidor.server_close()
        self.armazem.fechar()

    def get(self, caminho):
        con = http.client.HTTPConnection(*self.servidor.server_address, timeout=5)
        try:
            con.request("GET", caminho)
            r = con.getresponse()
            return r.status, r.read().decode()
        finally:
            con.close()

    def test_aba_pela_url(self):
        casos = {"/": "ativas", "/?aba=ativas": "ativas", "/?aba=recentes": "recentes",
                 "/?aba=recentes&x=1": "recentes", "/?aba=xyz": "ativas", "/?aba=": "ativas",
                 "/?aba=%22%3E%3Cb%3E": "ativas", "/?aba=recentes&aba=ativas": "recentes"}
        for caminho, esperada in casos.items():
            with self.subTest(caminho=caminho):
                status, html = self.get(caminho)
                self.assertEqual(200, status)
                self.assertIn(f'content="30; url=?aba={esperada}"', html)
                self.assertEqual(esperada == "ativas", "sessao-zeta-viva" in html)
                self.assertEqual(esperada == "recentes", "sessao-omega-fechada" in html)


if __name__ == "__main__":
    unittest.main()
