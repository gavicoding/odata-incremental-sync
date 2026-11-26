# odata-incremental-sync

[![CI](https://github.com/gavicoding/odata-incremental-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/gavicoding/odata-incremental-sync/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%20a%203.13-blue)
![Bancos](https://img.shields.io/badge/testado%20em-MySQL%208%20%7C%20PostgreSQL%2016%20%7C%20SQLite-informational)
[![Licença: MIT](https://img.shields.io/badge/licen%C3%A7a-MIT-green)](LICENSE)

[English](README.md) | Português

Sincronização incremental e conciliação só por chaves para APIs OData (ERPs, plataformas
fiscais, Dynamics, SAP Gateway): checkpoint que só avança depois que os dados foram gravados,
paginação segura, retentativa com espera crescente e um jeito barato de achar e preencher as
lacunas.

## O problema

A maioria das integrações OData é escrita do mesmo jeito: lê tudo de novo toda noite com
`$top`/`$skip`, sem retentativa e sem saber o que faltou. Fica lento, estoura o tempo limite nas
entidades grandes e deixa buracos sem ninguém perceber:

- um 429 ou 503 no meio e o job morre ou, pior, segue com metade dos dados;
- a API limita o `$top` em 100 e o laço para depois da primeira página;
- um registro alterado durante a execução desloca a janela do `$skip` e outro registro é pulado;
- o filtro manda `Z` (UTC) para um servidor que compara em hora local, e três horas de alterações
  nunca chegam;
- ninguém sabe quais notas faltam até alguém reclamar.

Esta biblioteca resolve cada um desses pontos com peças pequenas e testadas.

## Instalação

Precisa do Python 3.10 ou mais novo.

```bash
pip install "odata-incremental-sync @ git+https://github.com/gavicoding/odata-incremental-sync.git"
# com os drivers de banco:
pip install "odata-incremental-sync[mysql,postgres] @ git+https://github.com/gavicoding/odata-incremental-sync.git"
```

## Uso

### Sincronização incremental

```python
from datetime import timedelta
from zoneinfo import ZoneInfo
from odata_sync import ODataClient, SqliteSink, SqliteCheckpoint, sync_incremental

client = ODataClient("https://erp.exemplo.test/odata/", token="...", max_top=100)
sink = SqliteSink("erp.db", "invoices", "Key", create_table=True)
store = SqliteCheckpoint("erp.db")

result = sync_incremental(
    client,
    "Invoices",
    key_field="Key",
    cursor_field="ModifiedAt",
    store=store,
    sink=sink,
    lookback=timedelta(minutes=5),  # relê uma janela pequena; o upsert evita duplicar
    server_tz=ZoneInfo("America/Sao_Paulo"),
)
print(result.records, result.checkpoint_after)
```

O que acontece:

1. lê o checkpoint e pede `ModifiedAt ge (checkpoint - lookback)`, ordenado por
   `ModifiedAt, Key`;
2. entrega cada página ao destino (qualquer função: banco, fila, arquivo);
3. **só depois que o destino confirma** grava o último `ModifiedAt` daquela página como
   checkpoint. Se o destino der erro, o checkpoint fica onde estava e a próxima execução continua
   dali.

As páginas são percorridas **pela chave** (`ModifiedAt gt último or (ModifiedAt eq último and
Key gt última_chave)`) e não por `$skip`. Assim, alterações feitas no servidor durante a execução
não fazem pular registro. Há um teste exatamente para esse caso.

### Conciliação só por chaves

```python
from datetime import date
from odata_sync import reconcile_by_keys

report = reconcile_by_keys(
    client,
    "Invoices",
    key_field="Key",
    date_field="IssuedAt",
    start=date(2026, 11, 1),
    end=date(2026, 11, 10),
    existing_keys=sink.existing_keys,  # quais destas chaves eu já tenho?
    sink=sink,  # para onde vão os registros que faltam
    in_chunk=40,  # muitas APIs limitam o tamanho do filtro
    tz=ZoneInfo("America/Sao_Paulo"),  # os dias são cortados no calendário do negócio
)
print(report.table())
```

Para cada dia, lista **só as chaves** (`$select=Key`), pergunta quais você já tem e baixa os
registros completos só das que faltam, com `Key in (...)` em blocos. Listar chaves é barato;
baixar 4 notas em vez de 500 é o objetivo. Use `in_style="or"` para servidores sem o operador
`in` do OData 4.01, e `max_filter_chars` quando o limite é o tamanho do texto do filtro e não a
quantidade de chaves.

### Cliente

```python
client = ODataClient(
    base_url,
    token=None,
    auth=None,  # token bearer, ou qualquer auth do requests (ex.: ("usuario", "senha"))
    timeout=30,
    max_retries=5,
    backoff=1.0,
    max_backoff=60,
    max_top=100,  # páginas maiores que isso são reduzidas
)
for page in client.iter_pages("Customers", filter="Active eq true", orderby="Id", page_size=500):
    ...
```

- `iter_pages` segue o `@odata.nextLink` quando o servidor manda e usa `$top`/`$skip` quando não
  manda; funciona com respostas OData v4 (`value`) e v2 (`d.results`, `__next`).
- Tenta de novo em 429, 500, 502, 503, 504, erro de conexão e tempo esgotado, com espera
  crescente, respeitando o `Retry-After` (segundos ou data HTTP).
- 401 e 403 levantam `AuthError` na hora, com mensagem clara; outros 4xx levantam `ODataError`
  sem tentar de novo.
- Os valores da URL usam `%20`, nunca `+`, e o `+` do fuso horário é escapado.

### Checkpoints e destinos

| Checkpoint | Onde fica |
| --- | --- |
| `JsonFileCheckpoint(caminho)` | Arquivo JSON com gravação atômica (funciona em bucket do Cloud Storage montado) |
| `SqliteCheckpoint(caminho)` | Tabela no SQLite |
| `SqlCheckpoint(conn, "mysql" ou "postgres")` | Tabela no mesmo banco dos dados |
| `MemoryCheckpoint()` | Testes e execuções avulsas |

| Destino | Upsert |
| --- | --- |
| `SqliteSink(caminho ou conn, tabela, chave)` | `INSERT ... ON CONFLICT DO UPDATE` |
| `MySQLSink(conn, ...)` / `MySQLSink.connect(...)` | `INSERT ... ON DUPLICATE KEY UPDATE` |
| `PostgresSink(conn, ...)` / `PostgresSink.connect(conninfo, ...)` | `INSERT ... ON CONFLICT DO UPDATE` |

Sem `columns`, o destino guarda cada registro em JSON ao lado da chave, então dá para começar sem
desenhar tabela. Com `columns=[...]`, cada campo vai para sua coluna. Nomes de tabela e coluna
são validados e não permitem injeção de SQL.

## A armadilha do fuso horário

Servidores OData não concordam sobre o que uma data no filtro significa. Uns guardam hora local
e comparam o texto; outros respeitam o fuso; o OData v2 usa `datetime'...'` sem fuso nenhum.
Mandar `2026-11-26T10:00:00Z` para um servidor que guarda `ModifiedAt` em UTC-3 pede as
alterações desde as 07:00 locais, ou, se o servidor ignorar o `Z`, desde as 10:00 locais: três
horas de alterações puladas sem aviso.

Esta biblioteca **nunca aceita data sem fuso** e obriga a escolher o formato:

| `date_style` | Texto enviado para 13:00 UTC, `server_tz=America/Sao_Paulo` |
| --- | --- |
| `offset` (padrão) | `2026-11-26T10:00:00-03:00` |
| `utc` | `2026-11-26T13:00:00Z` |
| `local` | `2026-11-26T10:00:00` |
| `v2` | `datetime'2026-11-26T10:00:00'` |

`server_tz` também é o fuso usado para as datas que o servidor manda sem fuso. Teste uma vez à
mão: filtre um registro conhecido em cada formato e veja qual o traz.

## Saída de exemplo

A demo roda contra um servidor OData fictício local (`examples/fake_server.py`, notas
fictícias) que limita as páginas a 100 e recusa as duas primeiras requisições com 429:

```text
$ python examples/demo.py
1) First run: full load
   500 records in 5 pages, 8 requests (2 were 429 retries)
   checkpoint: 2026-11-10T22:25:00-03:00   rows in SQLite: 500
2) Second run: only what changed
   4 records read (lookback 5 min re-reads boundary rows), rows in SQLite: 500
3) Reconciliation: rows lost locally
   deleted 4 rows, rows in SQLite: 496
   day         remote  present  missing  fetched
   2026-11-01      37       36        1        1
   ...
   total          500      496        4        4
   rows in SQLite: 500, still missing: none
```

O servidor fictício também roda sozinho: `python examples/fake_server.py --port 8080`.

## Container e Google Cloud

O `Dockerfile` gera uma imagem pequena cujo ponto de entrada (`odata-sync-job`) faz uma
sincronização configurada por variáveis de ambiente (`ODATA_URL`, `ODATA_ENTITY`, `ODATA_KEY`,
`ODATA_CURSOR`, `TARGET=sqlite:///... | mysql://... | postgresql://...`, veja
`src/odata_sync/job.py`) e imprime um resumo em JSON.

O [docs/cloud-run.md](docs/cloud-run.md) é um passo a passo (em inglês) para rodar como **Cloud
Run Job** agendado pelo **Cloud Scheduler**, com o checkpoint no **Cloud SQL** (MySQL ou
PostgreSQL) ou num bucket do **Cloud Storage**. É um guia: o CI só gera a imagem e não tem conta
na nuvem.

## Testes

- Unitários (Python 3.10 a 3.13): paginação, nextLink, limite de `$top`, retentativa com
  `Retry-After`, 401/403, checkpoint (arquivo e SQLite), idempotência, destino que falha no meio,
  alterações durante a execução, conciliação com lacunas, limite de tamanho do filtro e fuso.
- Job de ponta a ponta com **MySQL 8** e **PostgreSQL 16** em service containers: sincronização
  completa contra o servidor fictício, contagem, segunda execução que não pode duplicar,
  checkpoint gravado no banco, atualizações e conciliação de linhas apagadas. As senhas são
  geradas a cada execução do CI.
- O ponto de entrada do container roda contra o PostgreSQL no CI, e a imagem Docker é gerada a
  cada push.

## Limitações conhecidas

- Exclusões no servidor não são detectadas pela sincronização incremental (o OData não tem um
  padrão de "excluídos desde"). A conciliação lista o que existe, então dá para comparar no
  sentido contrário também.
- A paginação pela chave precisa de um campo de corte e de uma chave que o servidor saiba
  ordenar e comparar. Chave composta ainda não é suportada.
- Se o servidor guarda mais casas decimais que microssegundos e duas linhas têm a mesma data, a
  paginação para com um erro claro em vez de entrar em laço.
- `$expand` e requisições em lote estão fora do escopo.

## Desenvolvimento

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check . && pytest
python examples/demo.py
```

## Licença

[MIT](LICENSE). Todos os dados dos exemplos e testes são fictícios.
