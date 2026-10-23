# pg-multi-mcp

[English](README.md) · **Português**

Servidor [MCP](https://modelcontextprotocol.io) local que dá a um assistente de IA acesso a **várias conexões de
banco nomeadas ao mesmo tempo**. Você diz *"lista as tabelas do banco de homologação"* e o assistente encontra a
conexão certa pelo nome ou apelido, reaproveita um pool já aberto e fecha os pools que ficaram ociosos.

Hoje suporta **PostgreSQL**. O núcleo (registro de conexões, resolução de nomes, ciclo de vida dos pools, reload
da configuração) não depende do motor: cada conexão tem um campo `engine` e o código específico do PostgreSQL fica
em um subpacote próprio, para que outros motores possam ser adicionados depois.

---

## Sumário

1. [Recursos](#recursos)
2. [Requisitos](#requisitos)
3. [Instalação](#instalação)
4. [Início rápido](#início-rápido)
5. [Configurando conexões](#configurando-conexões)
6. [Senhas e segredos](#senhas-e-segredos)
7. [Registrando o servidor no cliente MCP](#registrando-o-servidor-no-cliente-mcp)
8. [Opções de linha de comando](#opções-de-linha-de-comando)
9. [Variáveis de ambiente](#variáveis-de-ambiente)
10. [Referência das ferramentas](#referência-das-ferramentas)
11. [Como a conexão é escolhida](#como-a-conexão-é-escolhida)
12. [Ciclo de vida das conexões](#ciclo-de-vida-das-conexões)
13. [Editando a configuração com o servidor rodando](#editando-a-configuração-com-o-servidor-rodando)
14. [Modos de acesso e segurança](#modos-de-acesso-e-segurança)
15. [Exemplos de pedidos](#exemplos-de-pedidos)
16. [Solução de problemas](#solução-de-problemas)
17. [Estrutura do projeto e novos motores](#estrutura-do-projeto-e-novos-motores)
18. [Desenvolvimento](#desenvolvimento)
19. [Licença](#licença)

---

## Recursos

| Recurso | Descrição |
|---|---|
| Várias conexões nomeadas | Declaradas em arquivo JSON, no `.mcp.json` do cliente ou adicionadas pelo chat |
| Banco escolhido por chamada | Toda ferramenta de banco aceita `connection`; `use_connection` define uma padrão da sessão |
| Busca tolerante | Considera nome, aliases, tags e descrição, sem diferenciar maiúsculas e acentos, com sugestões "você quis dizer" |
| Modo de acesso por conexão | `restricted` (somente leitura, padrão) ou `unrestricted`, além de um teto global somente leitura |
| Pools lazy e reaproveitados | Abrem no primeiro uso, são reaproveitados, fecham por inatividade e por LRU acima do limite |
| Erros de conexão reais e rápidos | Senha errada, DNS ou porta fechada aparecem em segundos, não como timeout genérico |
| Reload da configuração | Edições no arquivo valem na próxima chamada; conexões inalteradas mantêm o pool |
| Ferramentas de análise PostgreSQL | Plano de execução (inclusive índices hipotéticos via HypoPG), health check, recomendação de índices, top queries |
| Tamanho de resposta controlado | `execute_sql` trunca resultados grandes (`max_rows`, padrão 500) |
| Sem vazamento de segredo | Senhas nunca aparecem em listagens nem em mensagens de erro |

## Requisitos

- Python **3.12+**
- Acesso de rede aos bancos configurados
- Opcional: [`uv`](https://docs.astral.sh/uv/), para rodar com `uvx` em vez de um virtualenv local
- Extensões PostgreSQL opcionais para algumas ferramentas: `pg_stat_statements` (top queries, análise de
  workload) e `hypopg` (índices hipotéticos)

## Instalação

### Opção A — virtualenv local

```powershell
git clone https://github.com/Snarloff/pg-multi-mcp.git
cd pg-multi-mcp
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

O comando fica em `.venv\Scripts\pg-multi-mcp.exe` (Windows) ou `.venv/bin/pg-multi-mcp` (Linux/macOS).

### Opção B — `uvx` direto do git (sem build manual)

O `uvx` baixa, compila e guarda em cache a partir do repositório na primeira execução:

```powershell
uvx --from git+https://github.com/Snarloff/pg-multi-mcp pg-multi-mcp --help
```

Assim ele acompanha a branch padrão. Para fixar uma versão, acrescente uma tag ou commit, por exemplo
`git+https://github.com/Snarloff/pg-multi-mcp@v0.1.0`, e todos rodam o mesmo código; para atualizar, troque-a.

## Início rápido

1. **Instale** (veja acima).
2. **Crie o arquivo de configuração** em `~/.pg-multi-mcp/connections.json`
   (Windows: `C:\Users\<você>\.pg-multi-mcp\connections.json`):
   ```json
   {
     "connections": {
       "local": {
         "url": "postgresql://postgres@localhost:5432/postgres",
         "access_mode": "unrestricted",
         "description": "Banco local de desenvolvimento",
         "aliases": ["dev"]
       }
     }
   }
   ```
3. **Forneça a senha** via pgpass ou variável de ambiente (veja [Senhas](#senhas-e-segredos)).
4. **Registre o servidor** no cliente MCP:
   ```powershell
   claude mcp add pg-multi-mcp --scope user -- C:\caminho\pg-multi-mcp\.venv\Scripts\pg-multi-mcp.exe
   ```
5. **Reinicie o cliente** e peça: *"lista minhas conexões de banco"* e depois *"lista os schemas do dev"*.

## Configurando conexões

As conexões podem vir de duas fontes, carregadas nesta ordem:

| Fonte | Use para | Persistida | Editável pelas ferramentas |
|---|---|---|---|
| 1. Arquivo `connections.json` | Seu registro principal de bancos | Sim | Sim (`add_connection`, `remove_connection`) |
| 2. Configuração do cliente `.mcp.json` (bloco `env`) | Conexões rápidas ou específicas de um projeto | Só na sessão | Só remove da sessão |

Se um nome existir nas duas, **a do `.mcp.json` prevalece na sessão**; a do arquivo continua intacta no disco e
volta se a sobreposição for removida. Se a variável `DATABASE_URI` estiver definida, ela vira uma conexão de sessão
chamada `default` (somente leitura).

### 1. Arquivo de configuração

- Local padrão: `~/.pg-multi-mcp/connections.json`.
- Troque com `--config <caminho>` ou com a variável `PG_MULTI_MCP_CONFIG`. `~` é expandido.
- Arquivo inexistente não é erro: o servidor inicia sem conexões.
- Modelo: [connections.example.json](connections.example.json).

```json
{
  "default": "local",
  "settings": {
    "idle_timeout_seconds": 600,
    "max_open_connections": 10
  },
  "connections": {
    "local": {
      "engine": "postgres",
      "url": "postgresql://postgres:${PG_LOCAL_PASSWORD}@localhost:5432/postgres",
      "access_mode": "unrestricted",
      "description": "Banco local de desenvolvimento",
      "aliases": ["dev", "localhost"],
      "tags": ["desenvolvimento"]
    },
    "relatorios_hml": {
      "url": "host=homolog.exemplo.com port=5432 dbname=relatorios user=leitura sslmode=require",
      "description": "Relatórios - homologação (somente leitura)",
      "aliases": ["relatorios hml"],
      "tags": ["homologacao", "relatorios"],
      "max_pool_size": 2,
      "query_timeout": 30
    }
  }
}
```

**Chaves de topo**

| Chave | Padrão | Descrição |
|---|---|---|
| `default` | `null` | Conexão usada quando a chamada não informa uma (veja [resolução](#como-a-conexão-é-escolhida)) |
| `settings.idle_timeout_seconds` | `600` | Fecha um pool após esse tempo ocioso, em segundos (`0` = nunca) |
| `settings.max_open_connections` | `10` | Máximo de pools abertos ao mesmo tempo; acima disso fecha o ocioso usado há mais tempo |
| `connections` | `{}` | Mapa nome da conexão → definição |

**Campos da conexão**

| Campo | Obrigatório | Padrão | Descrição |
|---|---|---|---|
| *(chave)* | sim | — | Nome da conexão: letras, números, `_`, `-`, `.`; até 63 caracteres |
| `url` | sim | — | URI libpq (`postgresql://usuario@host:porta/banco?sslmode=require`) ou DSN chave/valor (`host=... dbname=...`). Placeholders `${VAR}` são expandidos de variáveis de ambiente na hora de conectar |
| `engine` | não | `postgres` | Motor do banco (`postgresql` e `pg` são aceitos como apelidos) |
| `access_mode` | não | `restricted` | `restricted` = somente leitura; `unrestricted` = qualquer SQL, inclusive DML/DDL |
| `description` | não | `""` | Texto livre; usado na busca |
| `aliases` | não | `[]` | Nomes alternativos para o assistente encontrar a conexão |
| `tags` | não | `[]` | Rótulos de agrupamento/busca, como `producao`, `homologacao` |
| `max_pool_size` | não | `3` | Máximo de conexões neste pool |
| `query_timeout` | não | `30` | Timeout das queries, em segundos, nas conexões `restricted` |

Nomes e aliases precisam ser únicos entre todas as conexões depois de normalizados (maiúsculas, acentos e
separadores são ignorados): `Relatórios-HML`, `relatorios_hml` e `Relatorios HML` são a mesma chave.

### 2. `.mcp.json` (configuração do cliente)

Toda variável de ambiente `PG_MULTI_MCP_CONN_<NOME>` vira uma conexão chamada `<nome>` (em minúsculas). O valor é
uma URL/DSN ou um objeto JSON com os mesmos campos do arquivo. `PG_MULTI_MCP_DEFAULT` define a conexão padrão da
sessão. Modelo: [mcp.example.json](mcp.example.json).

```json
{
  "mcpServers": {
    "pg-multi-mcp": {
      "command": "C:\\caminho\\pg-multi-mcp\\.venv\\Scripts\\pg-multi-mcp.exe",
      "args": ["--access-mode", "restricted"],
      "env": {
        "PG_PROJETO_PASSWORD": "${PG_PROJETO_PASSWORD}",
        "PG_MULTI_MCP_CONN_PROJETO_DEV": "postgresql://app:${PG_PROJETO_PASSWORD}@localhost:5432/projeto_dev",
        "PG_MULTI_MCP_CONN_PROJETO_HML": "{\"url\": \"postgresql://leitura@homolog.exemplo.com/projeto\", \"aliases\": [\"homolog\"]}",
        "PG_MULTI_MCP_DEFAULT": "projeto_dev"
      }
    }
  }
}
```

> O `.mcp.json` costuma ser versionado no repositório do projeto. **Nunca coloque senha literal nele**: use
> `${VAR}` (expandido pelo cliente a partir do seu ambiente) ou pgpass.

## Senhas e segredos

Escolha uma das opções, da mais para a menos recomendada:

**1. Arquivo pgpass (nenhuma senha em configuração).** A libpq lê automaticamente quando a URL não tem senha.

| SO | Local |
|---|---|
| Windows | `%APPDATA%\postgresql\pgpass.conf` |
| Linux/macOS | `~/.pgpass` (precisa de `chmod 600`) |

Formato: `host:porta:banco:usuario:senha`, com `*` como curinga. Uma linha pode cobrir vários bancos:

```
*:5432:*:usuario_leitura:<senha>
```

**2. Placeholder `${VAR}`.** Coloque `${PG_LOCAL_PASSWORD}` na URL e defina a variável:

```powershell
setx PG_LOCAL_PASSWORD "<senha>"   # depois reinicie o cliente MCP para ele enxergar a variável
```

O placeholder é guardado como está e só é expandido na hora de conectar. Se a variável não existir, a chamada
falha com uma mensagem que cita o nome dela.

**3. Senha literal no `connections.json`.** Funciona, mas o arquivo é texto puro, legível por qualquer processo
rodando com o seu usuário. `add_connection` avisa quando vai gravar uma senha literal.

**Codificação na URL:** no formato URI, senhas com `@ : / # ? %` precisam de percent-encoding. O formato DSN
chave/valor (`password=...`) não precisa, desde que a senha não tenha espaços ou aspas.

## Registrando o servidor no cliente MCP

**Claude Code (escopo do usuário, vale em todos os projetos):**

```powershell
claude mcp add pg-multi-mcp --scope user -- C:\caminho\pg-multi-mcp\.venv\Scripts\pg-multi-mcp.exe --access-mode restricted
```

**Claude Code (escopo do projeto):** crie `.mcp.json` na raiz do projeto como em
[`.mcp.json` (configuração do cliente)](#2-mcpjson-configuração-do-cliente).

**Claude Desktop:** edite `%APPDATA%\Claude\claude_desktop_config.json` (Windows) ou
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) e adicione a mesma entrada em
`mcpServers`.

**Com `uvx`:**

```json
"pg-multi-mcp": {
  "command": "uvx",
  "args": ["--from", "git+https://github.com/Snarloff/pg-multi-mcp", "pg-multi-mcp", "--access-mode", "restricted"]
}
```

Depois de mudar a configuração do cliente, reinicie ou reconecte o servidor (no Claude Code: `/mcp`).

## Opções de linha de comando

| Opção | Padrão | Descrição |
|---|---|---|
| `--config CAMINHO` | `$PG_MULTI_MCP_CONFIG` ou `~/.pg-multi-mcp/connections.json` | Arquivo de conexões |
| `--access-mode restricted` | *(por conexão)* | Teto global: força **todas** as conexões a somente leitura, independentemente do arquivo |
| `--access-mode unrestricted` | | Igual a omitir: cada conexão usa o próprio `access_mode` |
| `--idle-timeout N` | do arquivo, senão `600` | Segundos até fechar um pool ocioso (`0` = nunca). Sobrepõe o arquivo |
| `--max-open N` | do arquivo, senão `10` | Máximo de pools abertos (LRU). Sobrepõe o arquivo |
| `--lock-config` | desligado | Desabilita `add_connection` e `remove_connection` |
| `--no-auto-reload` | desligado | Não recarrega o arquivo de conexões quando ele muda |
| `--transport stdio\|streamable-http` | `stdio` | Transporte MCP |
| `--host HOST` | `localhost` | Endereço do `streamable-http` |
| `--port PORTA` | `8000` | Porta do `streamable-http` |
| `--log-level NIVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` (logs vão para o stderr) |

Recomendado para bancos de produção: `--access-mode restricted --lock-config`.

## Variáveis de ambiente

| Variável | Descrição |
|---|---|
| `PG_MULTI_MCP_CONFIG` | Caminho do arquivo de conexões (igual a `--config`) |
| `PG_MULTI_MCP_CONN_<NOME>` | Declara uma conexão de sessão `<nome>`; valor é URL/DSN ou objeto JSON |
| `PG_MULTI_MCP_DEFAULT` | Conexão padrão da sessão (tem prioridade sobre o `default` do arquivo) |
| `DATABASE_URI` | Compatibilidade: vira uma conexão de sessão somente leitura chamada `default` |
| `PG_MULTI_MCP_INCLUDE_DTA_TRACE` | `true` para incluir o trace interno do recomendador de índices na resposta (padrão `false`) |
| Qualquer `${VAR}` que você referenciar | Expandida dentro das URLs na hora de conectar |

## Referência das ferramentas

Todas as ferramentas de banco aceitam `connection` opcional (nome ou alias). Toda resposta de banco começa com uma
linha como `[conexão: local | modo: unrestricted]`, então sempre fica claro onde o comando rodou.

### Gerenciamento de conexões

| Ferramenta | Argumentos | O que faz |
|---|---|---|
| `list_connections` | `search?` | Lista conexões: engine, destino (host/porta/banco/usuário, nunca a senha), modo de acesso, origem (`file`/`env`/`runtime`), status do pool, tempo ocioso, ativa/padrão. Mostra `config_error` se a última edição do arquivo for inválida |
| `use_connection` | `connection` | Valida a conexão (abre) e a torna a conexão ativa da sessão |
| `open_connection` | `connection?` | Abre ou reaproveita o pool e retorna banco, usuário, versão do servidor e latência |
| `close_connection` | `connection?`, `close_all=false` | Fecha um pool ou todos. A definição é mantida e reabre no próximo uso |
| `add_connection` | `name`, `url`, `access_mode="restricted"`, `description`, `aliases`, `tags`, `persist=true`, `test=true`, `overwrite=false`, `engine="postgres"` | Adiciona conexão. Com `test=true` conecta antes e não adiciona nada se falhar. Com `persist=true` grava no arquivo; senão existe só na sessão |
| `remove_connection` | `connection` | Fecha e remove a conexão (também do arquivo, se veio dele). Remover uma sobreposição da env traz de volta a do arquivo |
| `reload_connections` | — | Relê o arquivo agora e informa `added`, `changed`, `removed`, `unchanged` |

### Banco (PostgreSQL)

| Ferramenta | Principais argumentos | O que faz |
|---|---|---|
| `list_schemas` | — | Lista schemas e classifica em usuário/sistema |
| `list_objects` | `schema_name`, `object_type="table"` | Lista tabelas, views, sequences ou extensões |
| `get_object_details` | `schema_name`, `object_name`, `object_type="table"` | Colunas, constraints e índices de tabela/view; detalhes de sequence ou extensão |
| `execute_sql` | `sql`, `max_rows=500` | Executa SQL. Somente leitura em conexões `restricted`. Acima de `max_rows` o resultado é truncado e sinalizado |
| `explain_query` | `sql`, `analyze=false`, `hypothetical_indexes=[]` | Plano de execução; `analyze=true` executa a query para tempos reais; índices hipotéticos exigem HypoPG |
| `analyze_workload_indexes` | `max_index_size_mb=10000` | Recomenda índices a partir do workload em `pg_stat_statements` |
| `analyze_query_indexes` | `queries` (até 10), `max_index_size_mb=10000` | Recomenda índices para as queries informadas |
| `analyze_db_health` | `health_type="all"` | Health checks: `index`, `connection`, `vacuum`, `sequence`, `replication`, `buffer`, `constraint`, `all` (aceita lista separada por vírgula) |
| `get_top_queries` | `sort_by="resources"`, `limit=10` | Queries mais lentas ou mais custosas (`resources`, `mean_time`, `total_time`); exige `pg_stat_statements` |

## Como a conexão é escolhida

**Quando `connection` é informado**, a correspondência segue esta ordem:

1. Nome exato.
2. Nome ou alias normalizado: minúsculas, sem acento, sem espaço/`_`/`-`/`.`. Assim `"Relatórios"`, `"relatorios"`
   e `"RELATORIOS"` são iguais.
3. Não encontrado: o erro lista nomes parecidos ("Você quis dizer: ...") e correspondências parciais.

**Quando `connection` é omitido:**

1. A conexão ativa (definida por `use_connection`).
2. A padrão (`PG_MULTI_MCP_DEFAULT`, senão o `default` do arquivo).
3. A única conexão, se existir só uma.
4. Caso contrário, um erro pede que o assistente especifique e lista os nomes disponíveis. O servidor nunca
   escolhe sozinho entre vários bancos.

`list_connections(search="...")` busca em nomes, aliases, tags e descrições com a mesma normalização. O servidor
também instrui o assistente a procurar conexões dessa forma e a sempre passar `connection` explicitamente quando
houver mais de uma.

## Ciclo de vida das conexões

1. **Lazy:** nenhum pool abre na inicialização.
2. **Primeiro uso:** uma conexão de teste é feita (falha rápido com o erro real) e depois o pool é aberto.
3. **Reaproveitamento:** chamadas seguintes usam o mesmo pool.
4. **Fechamento por inatividade:** a cada 30 segundos, pools ociosos há mais de `idle_timeout_seconds` são
   fechados. Pools em uso nunca são fechados.
5. **Despejo LRU:** abrir um pool além de `max_open_connections` fecha o pool ocioso usado há mais tempo.
6. **Autocorreção:** erro de query invalida o pool, que é recriado na próxima chamada.
7. **Encerramento:** todos os pools são fechados quando o servidor termina.

As sessões aparecem no `pg_stat_activity` com `application_name = 'pg-multi-mcp'`. Um pool fechado reabre sozinho,
então `close_connection` serve para liberar conexões no servidor, não para bloquear acesso.

## Editando a configuração com o servidor rodando

Não é preciso reiniciar depois de editar o `connections.json`:

- **Automático:** antes de cada uso o servidor compara a data de modificação do arquivo e recarrega se mudou.
- **Manual:** `reload_connections` recarrega na hora e informa o que mudou.
- **Pools:** conexões inalteradas mantêm o pool aberto; alteradas e removidas têm o pool fechado.
- **Edição inválida:** se o arquivo ficar com JSON inválido (ou nome/alias duplicado), a versão anterior continua
  valendo e `list_connections` mostra `config_error` só com linha e coluna (nunca o conteúdo) até você corrigir.
- **Sem perder edições:** `add_connection`/`remove_connection` recarregam antes de gravar, então nunca sobrescrevem
  mudanças manuais, e ficam bloqueadas enquanto o arquivo estiver inválido.
- **Conexões do `.mcp.json`** não são afetadas pelo reload.

Mudanças no próprio `.mcp.json` só são lidas quando o cliente reinicia o servidor.

## Modos de acesso e segurança

**`restricted` (padrão)**

- O SQL é analisado (`pglast`) e conferido contra uma lista de comandos permitidos (basicamente `SELECT`,
  `EXPLAIN`, `SHOW` e cursores) e de funções permitidas.
- Tudo roda dentro de uma transação `READ ONLY`, então o próprio PostgreSQL rejeita qualquer escrita que passe.
- Queries são abortadas após `query_timeout` segundos.

**`unrestricted`**: qualquer SQL, inclusive `INSERT`, `UPDATE`, `DELETE`, `DROP`. Use só em bancos que são seus.

**Teto global:** `--access-mode restricted` força todas as conexões a somente leitura, mesmo que alguém coloque
`unrestricted` no arquivo ou adicione uma pelo chat. `--lock-config` também impede adicionar ou remover conexões.

**Recomendações**

- A garantia real é o privilégio do usuário no banco: conecte com um **usuário de banco somente leitura**.
- Os resultados das queries são enviados ao modelo de IA. Em bancos com dados pessoais, use acesso somente
  leitura, prefira consultas agregadas ou anonimizadas e garanta que existe base legal para tratar esses dados.
  Esta ferramenta não resolve a conformidade com proteção de dados (LGPD).
- Prefira `sslmode=require` (ou mais restrito) em bancos remotos.
- Senhas nunca aparecem na saída de `list_connections` nem em mensagens de erro.
- O servidor só envia dados ao cliente MCP que o iniciou; não chama nenhum outro serviço externo.

## Exemplos de pedidos

| Você diz | O que o assistente faz |
|---|---|
| "Quais bancos eu tenho?" | `list_connections()` |
| "Lista as tabelas do banco de relatórios em homologação" | `list_connections(search="relatorios")` → `list_objects(schema_name="public", connection="relatorios_hml")` |
| "A partir de agora usa o dev" | `use_connection("dev")`; as chamadas seguintes omitem `connection` |
| "Compara a tabela `usuarios` entre dev e homologação" | Duas chamadas `get_object_details`, uma por conexão |
| "Por que essa query está lenta em homologação?" | `explain_query(sql=..., connection="relatorios_hml")` |
| "O banco de homologação está saudável?" | `analyze_db_health(connection="relatorios_hml")` |
| "Adicionei um banco no arquivo" | Nada (reload automático), ou `reload_connections()` |
| "Fecha todas as conexões" | `close_connection(close_all=true)` |

## Solução de problemas

| Sintoma | Causa e solução |
|---|---|
| `fe_sendauth: no password supplied` | Sem senha na URL e sem pgpass. Adicione uma linha no pgpass ou um placeholder `${VAR}` |
| `password authentication failed` | Usuário/senha errados, ou a senha precisa de encoding no formato URI |
| `Variável(is) de ambiente não definida(s): X` | A URL usa `${X}` mas o servidor não enxerga a variável. Depois do `setx`, reinicie o cliente MCP |
| `connection timeout expired` / `connection refused` | Host ou porta inacessível: VPN, firewall ou porta errada |
| `Há várias conexões e nenhuma está ativa` | Várias conexões e nenhuma padrão/ativa: passe `connection`, use `use_connection` ou defina `default` |
| `Conexão 'x' não encontrada. Você quis dizer: ...` | Erro de digitação ou falta de alias; adicione um alias no arquivo |
| `config_error` em `list_connections` | O arquivo tem JSON inválido ou nome/alias duplicado; a mensagem mostra linha/coluna |
| `conflita com a conexão existente` / `usado por` | Duas conexões compartilham nome ou alias depois da normalização |
| Query recusada em modo restricted | O comando ou uma função não está na lista de leitura. Use `SELECT` ou uma conexão `unrestricted` |
| Mudanças no `.mcp.json` não aplicadas | O cliente só lê o arquivo ao (re)iniciar o servidor: reinicie ou reconecte |
| `WinError 32` ao reinstalar (`pip install -e .`) | O cliente MCP está rodando o `pg-multi-mcp.exe` e o Windows trava o arquivo. Pare ou desative o servidor no cliente antes (Claude Code: `/mcp`), reinstale e reconecte. Mudanças em `src/` não exigem reinstalar, só reiniciar o servidor |
| Precisa de mais detalhe | Rode com `--log-level DEBUG`; os logs vão para o stderr / log MCP do cliente |

## Estrutura do projeto e novos motores

```
src/
└── pg_multi_mcp/            Pacote Python (entry point: pg_multi_mcp:main)
    ├── __init__.py          main(): inicia o event loop (SelectorEventLoop no Windows)
    ├── server.py            Ferramentas MCP e opções de linha de comando
    ├── connections.py       Registro, resolução de nomes, pools, LRU, reaper, reload, persistência
    └── postgres/            Código específico do PostgreSQL
        ├── sql/             Driver, pool, validação de SQL somente leitura, extensões
        ├── explain/         Planos de execução e índices hipotéticos
        ├── index/           Recomendador de índices
        ├── database_health/ Health checks
        └── top_queries/     Relatórios do pg_stat_statements
tests/
├── unit/test_connections.py Registro, reload, env, persistência
├── unit/test_server.py      Roteamento e respostas das ferramentas
└── unit/postgres/           Testes dos módulos PostgreSQL
```

**Adicionando outro motor (roteiro):**

1. Adicione um valor em `Engine` em [connections.py](src/pg_multi_mcp/connections.py).
2. Crie `src/pg_multi_mcp/<motor>/` com um driver: pool, execução de queries e modo somente leitura.
3. Faça o `ConnectionManager` escolher pool/driver pelo `engine` da conexão.
4. Despache as ferramentas genéricas (`list_schemas`, `execute_sql`, ...) por engine; as exclusivas do PostgreSQL
   recusam outros motores.

## Desenvolvimento

```powershell
.\.venv\Scripts\python.exe -m pip install -e . pytest pytest-asyncio ruff==0.14.13
.\.venv\Scripts\python.exe -m pytest -q tests
.\.venv\Scripts\ruff.exe check src tests
```

Testes de integração que criam tabelas precisam de `PG_MULTI_MCP_TEST_DATABASE_URL` apontando para um banco
**descartável**; sem ela são pulados.

## Licença

MIT — veja [LICENSE](LICENSE).
