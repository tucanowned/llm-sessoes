# llm-sessoes

Gestor de sessões de CLIs de LLM: Claude Code, Codex e opencode.

Resolve dois problemas:

1. **Crash / reboot** — a máquina cai com várias sessões abertas (em worktrees, projetos
   diferentes…) e depois ninguém lembra quais eram nem de onde retomar cada uma.
2. **Aba fechada sem querer** — fechou a aba errada do terminal e não lembra qual sessão era,
   se estava em worktree ou na raiz.

A ideia: um **coletor** leve roda de tempo em tempo (cron), descobre quais sessões estão vivas e
registra em SQLite o que **abriu**, o que **fechou** e o que **morreu num crash**. Uma TUI (a
fazer) lê esse banco para buscar, selecionar e reabrir sessões.

## Arquitetura

```
coletor (cron, 2 min)  →  SQLite (~/.local/share/llm-sessoes/sessoes.db)  ←  TUI / CLI
        │
        └── provedores/   um módulo por ferramenta (claude, codex, …)
```

- **Provedores** (`src/llm_sessoes/provedores/`): cada ferramenta implementa a mesma interface —
  listar sessões vivas e montar o comando de resume. Tudo que depende de formato interno (não
  documentado) da ferramenta fica isolado aqui.
- **Coletor** (`coletor.py`): lógica pura de diff entre o estado anterior e o atual. Sem
  dependências externas — só stdlib — porque é a peça que *precisa* ter funcionado no dia do crash.
- **Banco** (`banco.py`): SQLite com `sessoes` (metadados), `vivas` (estado da última coleta),
  `eventos` (`abriu` / `fechou` / `crash`) e `meta`.

### Como o provedor Claude descobre as sessões

O Claude Code mantém um arquivo por processo em `~/.claude/sessions/<pid>.json` com `sessionId`,
`cwd`, `status`, `kind` e `procStart`. O provedor:

1. lê esses arquivos e considera só `kind == "interactive"`;
2. valida que o processo está vivo **e é o mesmo** comparando `procStart` com o `starttime` de
   `/proc/<pid>/stat` (protege contra reuso de PID e contra os arquivos órfãos que sobram de um crash);
3. como rede de segurança, varre `/proc` atrás de processos `claude` sem arquivo de sessão e
   aproveita o `--resume <id>` da linha de comando;
4. lê só o **final** do `.jsonl` da conversa (que pode ter centenas de MB) para pegar título
   (`ai-title`), última mensagem (`last-prompt`), branch e diretório de trabalho.

Dois diretórios são guardados por sessão:

- `dir_resume` — onde o processo foi iniciado; é de onde `claude --resume <id>` precisa rodar;
- `dir_trabalho` — onde a sessão está trabalhando de fato (ex.: um worktree).

### Como o provedor Codex descobre as sessões

O Codex não tem um arquivo por processo, mas cada processo `codex` mantém abertos (visível em
`/proc/<pid>/fd`) um `~/.codex/thread-writer-locks/<thread-id>.lock` por thread em que escreve e o
`rollout-*.jsonl` da conversa. O provedor:

1. lista processos `codex` com terminal e extrai os ids dos locks/rollouts abertos;
2. busca cada id na tabela `threads` de `~/.codex/state_<N>.sqlite` (somente leitura, maior `N`) e
   fica só com `thread_source = 'user'` não arquivada — threads auxiliares e sessões abertas sem
   nenhuma mensagem não aparecem ali e são ignoradas;
3. se o banco de estado sumir ou mudar de esquema, cai no `session_meta` (1ª linha) do rollout;
4. título = `name` (gerado pelo Codex) ou o começo da primeira mensagem; última mensagem vem da
   cauda do rollout, pulando os blocos de contexto injetado (`<environment_context>` etc.).

Validado com o codex-cli 0.154. Resume: `codex resume <thread-id>`.

### Como o provedor opencode descobre as sessões

O opencode (validado no 2.0.29) não tem um processo por sessão: o TUI é só um cliente do
**serviço em segundo plano** (`opencode serve --service`), e nada no `/proc` nem na API dele diz
qual sessão cada TUI está mostrando. Por isso, para o opencode, **aberta quer dizer em
andamento** (o que já vale, na prática, para sessões sem aba visível, como as de um tmux
destacado):

1. lê `~/.local/share/opencode/opencode.db` (somente leitura), tabela `session_v2`: sessões
   principais (não subsessões), não arquivadas, com atividade (`time_updated`) nos últimos
   **10 dias** (`LLM_SESSOES_OPENCODE_DIAS`). Abrir a sessão no TUI também conta como atividade;
2. título, diretório de trabalho (pode ser um worktree do próprio opencode, em
   `~/.local/share/opencode/worktree/…`) e a raiz do projeto (`project.worktree`, de onde se
   retoma); a branch é lida do `.git` do diretório; a última mensagem e o status vêm de
   `session_message` — o opencode grava uma linha `idle` ao fim de cada turno, então se ela é a
   mais recente a sessão está parada;
3. fecha quando passa do prazo sem atividade (o `fechou` é datado da **última atividade**, não da
   coleta que percebeu), quando é arquivada no opencode, ou à mão:

```bash
llm-sessoes opencode fechar ses_ebf1   # prefixo do id; não reabre até ter atividade nova
```

Reboot não vira crash para o opencode (a sessão continua no serviço). Sem processo, não há
terminal nem tmux associados; o resume é `opencode -s <id>`.

### Quando uma atualização muda o formato interno

Claude Code e Codex atualizam com frequência e nada do que os provedores leem é API pública. O
risco não é o provedor quebrar com erro — é ele passar a devolver lista vazia **em silêncio** e o
coletor concluir que todas as sessões fecharam. Proteções:

- cada provedor reporta **anomalias**: processo interativo vivo que ele não soube ler (um `claude`
  com terminal sem `sessions/<pid>.json`; um `codex` com terminal sem nenhum lock/rollout aberto;
  tabela `threads` ilegível). Sessão aberta sem mensagem **não** é anomalia;
- provedor com anomalia (ou que lançou erro) **não registra fechamentos** naquela rodada — aberturas
  continuam sendo registradas;
- os avisos vão para o log do cron e ficam no topo do `llm-sessoes status` até uma coleta limpa;
- versões novas de cada ferramenta são anotadas no log (`nota: codex: versão nova em uso: …`), para
  correlacionar uma quebra com a atualização que a causou.

### Detecção de crash

A cada coleta o `boot_id` do kernel (`/proc/sys/kernel/random/boot_id`) é gravado. Se ele mudou
e havia sessões vivas na coleta anterior, todas viram evento `crash`, datado da última coleta
em que foram vistas.

## Uso

Sem instalação — só Python 3.11+ (stdlib). A TUI é a exceção (ver abaixo).

```bash
bin/llm-sessoes coletar          # uma coleta (é o que o cron chama)
bin/llm-sessoes status           # sessões vivas segundo a última coleta
bin/llm-sessoes fechadas         # fechadas nas últimas 24h (--horas N)
bin/llm-sessoes crash            # sessões que estavam abertas no último crash
bin/llm-sessoes reabrir --crash  # reabre as pendentes do último crash, uma aba por sessão
bin/llm-sessoes tui              # interface: buscar, marcar e reabrir (requer Textual)
bin/llm-sessoes cron instalar    # agenda a coleta a cada 2 minutos (cron remover desfaz)
bin/llm-sessoes enviar           # manda o retrato ao painel central (se configurado)
bin/llm-sessoes central          # servidor do painel central (normalmente via Docker)
```

Para ter o comando no PATH: `ln -s "$PWD/bin/llm-sessoes" ~/.local/bin/llm-sessoes`.

O cron grava em `~/.local/state/llm-sessoes/coletor.log` — só quando algo muda (sessão abriu,
fechou, crash) ou quando um provedor falha. No WSL, o cron só sobe sozinho com `systemd=true` no
`/etc/wsl.conf` e o serviço habilitado (`systemctl is-enabled cron`).

Cada listagem mostra o comando pronto para retomar (`cd <dir> && claude --resume <id>`).

### Reabrir em abas do Windows Terminal

```bash
bin/llm-sessoes reabrir --crash               # pendentes do último crash
bin/llm-sessoes reabrir --fechadas --horas 2  # fechadas (e não reabertas) na janela
bin/llm-sessoes reabrir a78c 3fd6             # por prefixo do id (tem que ser único)
bin/llm-sessoes reabrir --crash -n            # só mostra o que abriria
```

Cada sessão vira um `wt.exe -w 0 new-tab` com título `<provedor>: <título>`. Sessão que já está
viva ou cujo diretório sumiu é pulada (com aviso). O comando entregue ao wt é só
`llm-sessoes _aba <provedor> <id>`: o diretório é lido do banco já dentro da aba, então caminho com
espaço ou `;` (que o wt trata como separador de comandos) nunca passa pela linha de comando do
Windows. Dentro da aba, o resume roda via shell interativo (`$SHELL -ic`), porque é o `.zshrc` que
põe `claude`/`codex` no PATH; quando a ferramenta sai, fica um shell aberto no mesmo diretório.

O `wt.exe` é procurado em `LLM_SESSOES_WT`, no PATH e em
`/mnt/c/Users/*/AppData/Local/Microsoft/WindowsApps/`. `reabrir.py` separa a montagem dos
argumentos (pura, testada) da execução (subprocess).

### Aviso de crash no shell e fechamento na hora

```bash
# ~/.zshrc (no fim) — custa ~40 ms e fala no máximo uma vez por boot
[[ -x ~/.local/bin/llm-sessoes ]] && ~/.local/bin/llm-sessoes aviso
```

Se o primeiro shell abrir antes da primeira rodada do cron, é o próprio `aviso` que faz a coleta que
registra o crash. Crash com mais de 7 dias não é mais lembrado. Nunca imprime erro nem devolve
código diferente de zero — não pode atrapalhar o shell.

```jsonc
// ~/.claude/settings.json
"hooks": { "SessionEnd": [ { "hooks": [
  { "type": "command", "command": "~/.local/bin/llm-sessoes hook claude", "timeout": 10 } ] } ] }
```

Com o hook, o `fechou` de uma sessão do Claude Code é registrado no instante em que ela termina, em
vez de esperar até 2 minutos pela próxima coleta (e a coleta seguinte não duplica o evento). O
Codex não tem hook equivalente: continua só pelo coletor.

### TUI

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-tui.txt   # uma vez
# sem python3-venv: python3 -m venv --without-pip .venv && pip3 --python .venv/bin/python install -r requirements-tui.txt
llm-sessoes tui
```

O Textual é a única dependência externa e só a TUI o importa. O lançador continua no python do
sistema (o coletor não pode depender de venv); `llm-sessoes tui` se reexecuta com o `.venv` do
repositório quando o Textual não está disponível.

| Tecla | Ação |
|---|---|
| `1`-`4`, `tab` / `shift+tab` | abas: Abertas, Fechadas, Último crash, Todas |
| `/` | busca incremental (título, diretórios, branch, provedor; termos são E; ignora acento). `enter` volta à lista, `esc` limpa |
| `espaço` / `a` | marca a linha / marca ou desmarca todas as visíveis |
| `enter` | reabre as marcadas — ou a linha do cursor, se não houver marca. Acima de 5 abas pede confirmação |
| `h` | janela da aba Fechadas: 24h → 72h → 7d |
| `c` | copia o `cd … && <resume>` da linha |
| `d` / `r` / `q` | oculta o painel de detalhe / recarrega (automático a cada 30 s) / sai |

Cada sessão ocupa duas linhas: selo do provedor e título em cima; estado, diretório (`⑂` = worktree),
branch e horário embaixo. O tema (paleta própria, em `tui/app.py`) usa âmbar para o que pede ação,
verde-água para sessão viva e coral para crash. Abre na aba onde há o que fazer (crash pendente > fechadas > abertas). Sessão viva nunca é reaberta; a recém-reaberta fica como `abrindo…` até a próxima coleta
enxergá-la. Trocar de aba desmarca tudo, para nenhuma marca invisível virar aba de surpresa.
`tui/dados.py` (itens por aba e busca) é lógica pura, testada sem Textual; os testes de
`tests/test_tui_app.py` usam o pilot do Textual e são pulados no python do sistema — rode a suíte
com `.venv/bin/python` para incluí-los.

Banco em `~/.local/share/llm-sessoes/sessoes.db` (sobrescreva com `LLM_SESSOES_DB`).

## Painel central

Cada máquina envia o estado das suas sessões para um servidor no orbit, que mostra numa página
as sessões ativas e as recentes de todas as máquinas — com o comando pronto para retomar
cada uma por ssh.

```
máquina A ─┐  POST /api/v1/relatorio (Bearer)
máquina B ─┼──────────────────────────────────→  llm-sessoes central  →  página / + /api/v1/resumo
máquina C ─┘  retrato completo a cada coleta       (SQLite em /data)       (widget do Homepage)
```

### Cliente

Configuração por variáveis de ambiente ou, como fallback, em `~/.config/llm-sessoes/central.env`
(formato `CHAVE=valor`; a variável de ambiente ganha do arquivo):

```bash
# ~/.config/llm-sessoes/central.env
LLM_SESSOES_CENTRAL_URL=https://sessoes.orbit.diegolabs.dev
LLM_SESSOES_TOKEN=<um dos tokens do servidor>
# LLM_SESSOES_MAQUINA=notebook   # padrão: hostname. É o alvo do `ssh` no comando copiado
```

Sem URL, nada muda: `llm-sessoes enviar` não faz nada e sai com 0. Com URL, o envio acontece
sozinho **no fim de cada `coletar`** (cron) e **no hook `SessionEnd`** do Claude Code; o comando
`enviar` serve para testar à mão (sai com 1 e mostra o erro se falhar).

- Cada envio é um **retrato completo** (idempotente): sessões vivas, eventos das últimas 48 h
  (`abriu`/`fechou`/`crash`) e as anomalias dos provedores. O servidor substitui o estado da
  máquina, então um envio perdido se corrige no seguinte.
- Timeout de 5 s. Falha de rede vira **uma linha no `coletor.log`** e nunca derruba a coleta
  nem muda o código de saída dela.
- **tmux**: a coleta lê `TMUX` e `TMUX_PANE` em `/proc/<pid>/environ` do processo da sessão e
  resolve com `tmux -S <socket> display -p -t <painel> '#S:#I'`. Fica gravado no banco local
  (a TUI mostra no detalhe) e vai no relatório.
- Status (`trabalhando`/`parada`) só existe para o Claude Code (`busy`/`idle` do arquivo de
  sessão); "última atividade" é o mtime do arquivo da conversa.

Formato do relatório (`versao_esquema: 1`, horários em epoch UTC):

```jsonc
{ "versao_esquema": 1, "maquina": "notebook", "boot_id": "…", "coletado_em": 1763400000,
  "versao_cliente": "0.2.0",
  "vivas":   [ { "provedor": "claude", "id": "…", "titulo": "…", "ultima_msg": "… (≤300)",
                 "dir_resume": "…", "dir_trabalho": "…", "branch": "…",
                 "status": "trabalhando" | "parada" | null, "inicio": 0, "ultima_atividade": 0,
                 "tmux": { "sessao": "dev", "janela": 2, "painel": "%3" } | null } ],
  "eventos": [ { "tipo": "abriu" | "fechou" | "crash", "quando": 0, /* + campos da sessão */ } ],
  "anomalias": [ "codex: processo codex pid=… sem thread-writer-lock …" ] }
```

### Servidor

`llm-sessoes central` — `ThreadingHTTPServer` na porta 8080, SQLite em `/data/central.db`, só
stdlib. Variáveis: `LLM_SESSOES_TOKENS` (obrigatória; tokens aceitos, separados por vírgula),
e opcionalmente `LLM_SESSOES_PORTA`, `LLM_SESSOES_ENDERECO`, `LLM_SESSOES_CENTRAL_DB` (ou
`--porta`, `--endereco`, `--banco`).

| Rota | |
|---|---|
| `POST /api/v1/relatorio` | retrato de uma máquina. `401` sem token válido, `413` acima de 1 MB (recusado pelo `Content-Length`, sem ler o corpo), `400` se o formato não bate |
| `GET /?aba=ativas` \| `GET /?aba=recentes` | a página (auto-refresh de 30 s na mesma aba, tema Catppuccin); `?aba` ausente ou inválido = `ativas` |
| `GET /api/v1/resumo` | `{ativas, maquinas_online, maquinas_total, crashes_24h}` |
| `GET /healthz` | `200 ok` |

A página tem duas abas globais, **Ativas** e **Fechadas e crash**, com os contadores somados de
todas as máquinas (as ativas só das online: as de uma offline são um retrato velho); a segunda fica vermelha quando houve crash nas últimas 24 h. São links comuns
gerados no servidor (sem JavaScript), e a aba fica na URL, então o auto-refresh a mantém. Dentro
de cada aba há um bloco por máquina só com o conteúdo daquela aba; máquina sem nada na aba vira
uma linha compacta. O cabeçalho de cada PC mostra `2 ativas · 5 fechadas (1 crash)`, com
link para a outra aba. A página tem um bloco por máquina — **online/offline** (offline = sem relatório há mais de
5 min, pelo relógio do servidor) e "último envio há X" —, com as anomalias em destaque, as
sessões ativas (título, projeto/worktree/branch, `trabalhando`/`parada` e há quanto tempo,
tmux) e as fechadas/crash das últimas 48 h que não foram reabertas. O botão copia:

- sessão viva num tmux: `ssh <maquina> -t tmux attach -t <sessao>`;
- demais: abre a sessão num **tmux novo**, por um shell interativo:
  `ssh <maquina> -t tmux new -A -s claude-a78c1f02 -c <dir_resume> '${SHELL:-zsh} -ic "claude --resume <id>"'`
  (`codex resume <id>` para o Codex). O shell interativo é necessário porque, por ssh, o
  `claude` só entra no PATH (e os segredos só são carregados) pelo `.zshrc`; dentro do tmux a
  sessão sobrevive à queda do ssh; e o `-A` faz um segundo clique reconectar em vez de abrir
  outra. As aspas são montadas com `shlex` em duas camadas (shell local → ssh → shell remoto),
  então diretório com espaço ou aspas funciona.

Todo texto vindo das sessões é escapado, e a página sai com uma CSP que só permite o script e
o estilo dela (nonce por resposta). O nome da máquina só aceita `[A-Za-z0-9._-]` (vira alvo de
`ssh`). Eventos com mais de 30 dias são apagados. Ativas de máquina offline não contam no
resumo (são um retrato velho).

**A página e o `/api/v1/resumo` não têm autenticação** — mostram títulos e caminhos das
sessões. Exponha só na rede do Tailscale.

#### Deploy (Docker)

```bash
cp compose.example.yml compose.yml
echo "LLM_SESSOES_TOKENS=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" > .env
docker compose up -d --build
```

A imagem (`python:3.12-slim`, usuário `sessoes` sem root, `VOLUME /data`, `EXPOSE 8080`) tem
healthcheck no `/healthz`. Widget do Homepage:

```yaml
- Sessões LLM:
    href: https://sessoes.orbit.diegolabs.dev
    widget:
      type: customapi
      url: http://llm-sessoes:8080/api/v1/resumo
      refreshInterval: 30000
      mappings:
        - { field: ativas, label: Ativas }
        - { field: maquinas_online, label: Online }
        - { field: crashes_24h, label: Crashes 24h }
```

## Testes

Rode da raiz do repositório. O `PYTHONPATH=src` é necessário porque o pacote fica em `src/`
e não é instalado.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v          # sem os testes da TUI
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v # suíte completa (com Textual)
```

## Roadmap

- [x] Coletor + SQLite + provedor Claude
- [x] Provedor Codex
- [x] TUI (Textual): abas Abertas / Fechadas / Último crash / Todas, busca, multi-seleção, reabrir
- [x] Reabrir em abas do Windows Terminal (`wt.exe new-tab`) — módulo `reabrir` + CLI
- [x] Aviso de crash no primeiro shell após o boot (`llm-sessoes aviso`)
- [x] Hook `SessionEnd` do Claude Code para registrar fechamento na hora (`llm-sessoes hook claude`)
- [x] Provedor opencode (aberta = em andamento; fechamento manual ou por inatividade)
- [x] Painel central: `llm-sessoes enviar` em cada máquina + `llm-sessoes central` no orbit (Docker)
