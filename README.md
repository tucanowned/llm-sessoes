# llm-sessoes

Gestor de sessões de CLIs de LLM: Claude Code e Codex.

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
