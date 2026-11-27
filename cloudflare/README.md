# Nuvem-INMET no Cloudflare

O sistema reúne observações horárias das estações automáticas do INMET.
O Cloudflare consulta arquivos públicos via HTTP a cada cinco horas,
indexa as medições no Workers KV e oferece consultas por estação e dia UTC.

## Consultar

A API retorna as 24 horas UTC de uma estação e de uma data. Use um código WMO,
por exemplo `A807` para Curitiba, e uma data no formato `AAAA-MM-DD`.

| Rota | Resposta |
| --- | --- |
| `GET /health` | Disponibilidade da leitura no KV |
| `GET /v1/fields` | Nome, código do INMET e unidade das 17 variáveis |
| `GET /v1/observations?station=A807&date=2026-01-01` | Temperatura e umidade em JSON |
| `GET /v1/observations?station=A807&date=2026-01-01&fields=all` | As 17 variáveis em JSON |
| `GET /v1/observations?station=A807&date=2026-11-24&fields=all` | Consulta de uma estação do lote nacional recebido pelo BDMEP |
| `GET /v1/observations?station=A843&date=2026-11-23&fallback=wis2` | Contingência WIS2 quando falta uma observação oficial indexada |

O parâmetro `fields` aceita nomes separados por vírgula, na ordem desejada.
Use `format=csv` para obter as mesmas linhas em CSV.

```bash
curl -fS 'http://127.0.0.1:8787/v1/observations?station=A807&date=2026-01-01&fields=wind_speed_m_s,wind_direction_deg,wind_gust_m_s,precipitation_mm&format=csv'
```

A API devolve os decimais como texto. `null` no JSON ou célula vazia no CSV
indica medição ausente. Na coleta, campos vazios, `Null` textual e os
marcadores `9999` e `-9999` viram lacunas do sensor. Os demais sensores da
mesma hora continuam disponíveis. `recorded: false` indica que o índice
não contém registro para aquela hora.
`day_not_found` significa dia ausente e `station_not_found` significa estação
ausente num dia existente. A precipitação é a da hora. Os valores de pressão
são ao nível da estação. A radiação horária está em kJ/m².

As consultas dão prioridade aos lotes oficiais e às medições do portal
já indexadas. O espelho completa horas e campos ausentes. Para respostas
do espelho, `X-INMET-Source` indica `agencia-sertao`. Quando o espelho
complementa uma resposta do portal ou do BDMEP, o cabeçalho
`X-INMET-Supplement` identifica seu uso. `X-INMET-Coverage` e
`X-INMET-Observed-Hours` descrevem a cobertura horária da estação.
Consumidores que aceitam a contingência WIS2 podem usar `fallback=wis2`.

[Catálogo da API](http://127.0.0.1:8787/v1/fields) · [Fonte INMET](https://portal.inmet.gov.br/dadoshistoricos)

## Coleta e armazenamento

O Cron verifica janelas de cinco horas corridas contadas a partir de
25/11/2026 às 13h UTC. Cada janela cria apenas uma instância do espelho.
O Worker consulta por HTTPS o catálogo nacional do Portal Tempo da Agência
Sertão, que republica observações do INMET. O catálogo traz a última medição
disponível por estação. O sistema lê, em ordem rotativa, até 24 CSVs públicos
por janela para completar a série horária dos oito dias UTC mais recentes.
As consultas dos CSVs são seriais, com quatro segundos entre elas.
[Estações do Portal Tempo](https://agenciasertao.com/tempo/estacoes/) · [Metodologia do Portal Tempo](https://agenciasertao.com/tempo/metodologia/)

O Workflow usa o código da estação, a data UTC, os 17 campos e as unidades
do CSV. Ele preserva a precisão decimal dos arquivos, mantém medições
anteriores quando a fonte omite um campo e guarda os dados em
`observations/mirror/v1/AAAA-MM-DD.bin`. O índice NIM2 tem 10 bytes de
cabeçalho, entradas de 9 bytes por estação e um bloco gzip por estação.
O estado no KV guarda o último código visitado e o ETag do catálogo.
HTTP 304 reaproveita a lista. HTTP 401, 403 ou 429 pausa a origem e
segue o prazo informado em `Retry-After`. As demais falhas aparecem no
resultado da rodada. A cobertura depende das estações com série recente
no espelho e aumenta à medida que os CSVs são lidos.

Em uma a cada cinco janelas, a recuperação tenta a rota nacional do próprio
INMET para uma lacuna dos últimos 30 dias. A recuperação dá vez às datas
anteriores para que uma falha persistente em um dia não prenda a fila.
Os lotes oficiais têm precedência. Se a requisição nacional falhar, o WIS2
oficial pode preencher séries parciais. O WIS2 usa os códigos WIGOS
vinculados ao cadastro por nome e coordenadas e continua disponível
mediante `fallback=wis2`. [Observações automáticas no WIS2](https://wis2bra.inmet.gov.br/oapi/collections/urn:wmo:md:br-inmet:synop) · [Dados históricos oficiais](https://portal.inmet.gov.br/dadoshistoricos)

O Worker principal usa Workflows e Workers KV para a coleta e a consulta.
O indexador independente do BDMEP continua disponível para quem configurar
a importação pelo portal. As tabelas e os arquivos nacionais já indexados
permanecem consultáveis pelo mesmo contrato.
[Portal oficial BDMEP](https://bdmep.inmet.gov.br/)

## Verificação do protocolo

`npm run test:workflow` usa Workerd e respostas simuladas. Ele verifica o
Cron, a coleta HTTP, a publicação e a leitura das medições. Também exercita
a retomada do fluxo independente de importação do BDMEP.

## Publicação e acompanhamento

No diretório `cloudflare/`, use Node.js 22 ou superior e o Wrangler
fixado no projeto. Configure o namespace `INMET_DAYS` na conta de destino.

```bash
npm ci
npm test
npm run test:workflow
npm run build
npm run deploy
```

Depois do deploy, confira a rota `/health`, uma consulta ao acervo e o
Cron `0 * * * *` no painel do Worker. A janela UTC aparece no final do
identificador do Workflow. O resultado informa o horário do catálogo,
a quantidade de estações, a cobertura dos dias publicados e os erros de
leitura. `indexed` indica uma rodada indexada. `partial` indica que parte
das leituras precisou ficar para outra janela.

## Carga histórica

Use os arquivos horários das estações automáticas do INMET. O código
preserva as medições disponíveis e as lacunas. Ao redistribuir dados de
um espelho, conserve a atribuição e os termos da fonte.

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
Para carregar na conta Cloudflare, substitua `--local` por
`--remote --config wrangler.local.jsonc` e confira antes se a data já existe.
