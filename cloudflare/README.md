# Nuvem-INMET no Cloudflare

Um Worker recebe consultas e dispara a coleta às **05h de Brasília** (08h UTC).
O Cloudflare Workflows consulta uma vez o dia UTC anterior na rota nacional
do INMET para estações automáticas. O Workers KV guarda um lote por dia.
Cada implantação usa o Worker, o Workflow e o KV da conta que a publica.

## Consultar

A API retorna as 24 horas UTC de uma estação e de uma data. Use um código WMO,
por exemplo `A807` para Curitiba, e uma data no formato `AAAA-MM-DD`.

| Rota | Resposta |
| --- | --- |
| `GET /health` | Disponibilidade da leitura no KV |
| `GET /v1/fields` | Nome, código do INMET e unidade das 17 variáveis |
| `GET /v1/observations?station=A807&date=2026-01-01` | Temperatura e umidade em JSON |
| `GET /v1/observations?station=A807&date=2026-01-01&fields=all` | As 17 variáveis em JSON |

O parâmetro `fields` aceita nomes separados por vírgula, na ordem desejada.
Use `format=csv` para obter as mesmas linhas em CSV.
O exemplo usa o ambiente local com um lote já carregado. Para consultar
uma implantação publicada, use o endereço informado pelo Wrangler.

```bash
curl -fS 'http://127.0.0.1:8787/v1/observations?station=A807&date=2026-01-01&fields=wind_speed_m_s,wind_direction_deg,wind_gust_m_s,precipitation_mm&format=csv'
```

A API devolve os decimais como texto. `null` no JSON ou célula vazia no CSV
indica medição ausente. `recorded: false` indica que a hora inteira falta.
`day_not_found` significa dia ausente e `station_not_found` significa estação
ausente num dia existente. A precipitação é a da hora. Os valores de pressão
são ao nível da estação. A radiação horária está em kJ/m².

[Definições das variáveis](src/fields.json) · [Fonte INMET](https://portal.inmet.gov.br/dadoshistoricos)

## Coleta e armazenamento

O agendamento cria uma instância com o dia como identificador. Se o dia já
está publicado, a instância encerra. O Workflow mantém a resposta original
por 12 horas em uma chave temporária do KV. Cada etapa lê e normaliza uma
fatia de até 64 KiB do JSON. Os registros são agrupados, os dados de cada
estação são compactados em gzip e o resultado é publicado em uma única
chave `observations/v2/AAAA-MM-DD.bin`. A leitura faz um acesso ao KV e usa
o índice para descompactar somente a estação consultada.

O índice `NIM2` tem 10 bytes. São a assinatura, o dia UTC em dias desde
1970-01-01 e a contagem de estações. Cada entrada ocupa 9 bytes com código
ASCII de 5 posições e deslocamento de 4 bytes. Inteiros usam ordem de bytes
big-endian. Cada bloco gzip contém linhas `[hora, ...medições]` com as
colunas na ordem de [fields.json](src/fields.json).

O Workflow mantém o dia fora da consulta se a API do INMET ficar indisponível
ou entregar JSON inválido. O histórico publicado continua disponível. O
painel de Workflows mostra o erro da execução e permite disparar um dia em
atraso. A coleta direta cobre estações automáticas. A fonte é o INMET e a
disponibilidade dos sensores varia por estação e hora.

[Limites de Workers](https://developers.cloudflare.com/workers/platform/limits/) · [Limites de Workflows](https://developers.cloudflare.com/workflows/reference/limits/) · [Limites de KV](https://developers.cloudflare.com/kv/platform/limits/)

## Teste local

No diretório `cloudflare/`, use Node.js 22 ou superior e a versão do Wrangler
fixada no projeto.

```bash
npm ci
npm test
npm run build
npm run dev
```

Acesse `http://127.0.0.1:8787/health` para conferir o KV local. O
`/v1/fields` lista as variáveis sem precisar carregar dados. Para
consultar uma estação, carregue um lote conforme a seção de histórico.

## Publicação e reprocessamento

Use `npx wrangler whoami` para conferir a conta de destino. Faça uma
cópia local da configuração antes de publicar. O Wrangler cria o namespace KV
na conta de destino e grava o ID nessa cópia, ignorada pelo Git.

```powershell
Copy-Item wrangler.jsonc wrangler.local.jsonc
npm run deploy -- --config wrangler.local.jsonc
```

Em Bash, use `cp wrangler.jsonc wrangler.local.jsonc` para copiar o arquivo.
Reutilize essa cópia nos próximos deploys. Ajuste os nomes do Worker e
do Workflow nela se a conta já tiver recursos com os nomes de exemplo.

Confira `/health`, a leitura de um dia com dados e o Cron `0 8 * * *`
depois da publicação. Para reprocessar uma data em atraso, abra o Workflow
no painel e inicie uma instância com o dia UTC desejado em JSON, por
exemplo `{"day":"2026-04-20"}`. O painel informa o resultado da tentativa.

[Provisionamento automático do Wrangler](https://developers.cloudflare.com/workers/wrangler/configuration/#automatic-provisioning)

## Carga histórica

Use os arquivos horários das estações automáticas do INMET. O código
interpreta as medições da fonte e preserva as lacunas. Ao compartilhar dados
de um espelho, conserve a atribuição e os termos da fonte.

[Arquivos históricos do INMET](https://portal.inmet.gov.br/dadoshistoricos)

O conversor aceita ZIP oficial, diretório de CSVs por estação ou CSV
consolidado. Execute na raiz do repositório com o pacote Python instalado.
O dia final é exclusivo. O manifesto informa o total de estações, linhas,
bytes e SHA-256 de cada lote.

```bash
python cloudflare/scripts/pack-archive.py data/2026.zip --start 2026-01-01 --end 2026-11-01 --output data/lotes
cd cloudflare
npx wrangler kv key put observations/v2/2026-01-01.bin --binding INMET_DAYS --path ../data/lotes/2026-01-01.bin --local
```

Use a chave correspondente a cada data do manifesto em uma carga nova.
Para importar na conta Cloudflare, use `--remote --config wrangler.local.jsonc`
no lugar de `--local` e confira primeiro se a data já existe no destino.
