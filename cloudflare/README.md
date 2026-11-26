# Nuvem-INMET no Cloudflare

O sistema coleta **as tabelas horárias das estações automáticas** e mantém
uma segunda fonte, o arquivo nacional do BDMEP. O próprio Cloudflare executa
o navegador, organiza as tentativas e grava as observações no Workers KV.
Cada consulta identifica uma estação e um dia UTC.

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

As consultas leem primeiro os lotes históricos em `observations/v2/`.
Para datas coletadas pelo BDMEP, o arquivo nacional aparece assim
que o Worker termina a indexação. Quando já existe uma tabela individual
do portal, a consulta preserva suas medições e usa o BDMEP para preencher
as horas ou variáveis solicitadas que ainda estejam vazias.
`X-INMET-Source` identifica o canal `portal`, `bdmep` ou `wis2`.
`X-INMET-Coverage` e `X-INMET-Observed-Hours` informam a cobertura da estação.
Consumidores que aceitam a contingência WIS2 podem adicionar `fallback=wis2`.

[Catálogo da API](http://127.0.0.1:8787/v1/fields) · [Fonte INMET](https://portal.inmet.gov.br/dadoshistoricos)

## Coleta e armazenamento

### Tabelas por estação

O coletor principal percorre o catálogo das cinco regiões. Para cada estação
e dia, o Browser Run abre a página pública da tabela. A própria página faz
a solicitação horária. O coletor aguarda a chegada do corpo, confere o código,
o dia e a hora de cada registro e aproveita os valores antes do arredondamento
da tela. Uma linha vazia permanece como lacuna. Vento ou precipitação com
valor zero são medições válidas. Se o catálogo estiver indisponível, o
coletor usa a última lista válida ou a lista distribuída com o código e
identifica a cobertura como provisória.

A sessão de navegador é reutilizada dentro do lote e encerrada ao fim.
O progresso fica no KV da implantação sob `portal/survey/v1/active`.
Se a origem ou o Browser Run informar HTTP 429, o coletor respeita o prazo
de `Retry-After`. A disponibilidade do portal, o tempo de navegador contratado
e a quantidade de estações determinam quanto da rede pode ser coberto em
uma rodada. [Tabela pública de exemplo](https://tempo.inmet.gov.br/tabela/mapa/A040/2026-11-26) · [Browser Run](https://developers.cloudflare.com/browser-run/script/)

Para acompanhar a execução pela sua conta Cloudflare, consulte o estado do
coletor no diretório `cloudflare/` com a configuração local da implantação.

```bash
npx wrangler kv key get portal/survey/v1/active --binding INMET_DAYS --remote --text --config wrangler.local.jsonc
```

`planned` conta as estações do catálogo e `measured` conta as que forneceram
ao menos uma medição. `remaining` indica quantas ainda precisam de dados.
Em uma pausa, `reason`, `lastError` e `resumeAt` permitem distinguir a causa
e consultar o horário previsto para a retomada. Para conferir as horas de uma
estação específica, consulte `/v1/observations` e `X-INMET-Observed-Hours`.

### Arquivo nacional do BDMEP

O Cron roda no início de cada hora UTC. Nas janelas de cinco horas corridas,
contadas a partir de 25/11/2026 às 13h UTC, o Worker confere o acervo e
pode abrir **um único pedido** no BDMEP. Enquanto um pedido aguarda
confirmação ou arquivo, o agendamento aguarda. Uma entrega repetida do Cron
reutiliza o identificador da janela.

Às 08h UTC (05h de Brasília), o dia anterior passa a ser elegível para
fechamento. A prioridade é o dia elegível mais recente. A partir desse
horário, o sistema pode também colher o dia corrente uma vez, em caráter
provisório. Depois vêm o dia anterior ao último elegível e a lacuna mais
antiga dos últimos 30 dias. Um lote nacional definitivo já indexado por
qualquer dos dois formatos encerra a pendência. Uma estação individual
indexada ou uma contingência parcial continua permitindo a coleta nacional.

O índice `NIM2` tem 10 bytes. São a assinatura, o dia UTC em dias desde
1970-01-01 e a contagem de estações. Cada entrada ocupa 9 bytes com código
ASCII de 5 posições e deslocamento de 4 bytes. Inteiros usam ordem de bytes
big-endian. Cada bloco gzip contém linhas `[hora, ...medições]` com as
colunas na ordem de [fields.json](src/fields.json).

O Cloudflare Browser Run seleciona a rede automática nacional e verifica
que o formulário carregou as variáveis e as estações das cinco regiões
antes de confirmar. Se o portal recusar a navegação ou deixar o catálogo
vazio, o Workflow espera cinco minutos para repetir o formulário e
admite outra tentativa dez minutos depois. Se as três tentativas
falharem antes do envio, libera o recebimento e conserva a pendência
para a próxima janela. Se a resposta se perder no envio, o Workflow aguarda o recibo por
e-mail para evitar um segundo pedido. O Cloudflare Email Routing entrega os e-mails do INMET ao
Worker e o Workflow confirma o pedido e baixa o arquivo oficial da fila.
A confirmação e o download admitem três novas tentativas com esperas
de cinco, dez e vinte minutos. Cada retomada usa o mesmo pedido recebido.
Se essas tentativas se esgotarem, a próxima janela de cinco horas retoma
a etapa com erro na mesma instância, conservando o recibo e o formulário
já concluído. A validade original do recebimento não é prorrogada.
O arquivo fica temporariamente no KV. Em cada execução do indexador,
uma leitura carrega o ZIP, e cada bloco é validado e compactado no mesmo
passo. Se o Workflow for retomado, ele pode reler o ZIP para continuar
do último passo concluído. O índice NIM2 guarda as medições disponíveis
com os decimais da fonte. A publicação grava um lote em
`observations/bdmep/v1/AAAA-MM-DD.bin` e remove o ZIP temporário. Se
o ZIP do dia já estiver no KV, a janela pode indexá-lo diretamente.
[Portal oficial BDMEP](https://bdmep.inmet.gov.br/)

As rotas de consulta também servem o acervo histórico em `observations/v2/`
e os lotes WIS2 já recebidos. O agendador utiliza `INMET_COLLECTOR` para
as tabelas e `INMET_BDMEP` para o arquivo nacional.

A contingência WIS2 usa o mesmo formato binário, na chave
`observations/wis2/v1/AAAA-MM-DD.bin`. Ela verifica unidades e intervalos
para temperatura instantânea, máxima e mínima, umidade, vento, rajada,
direção, pressão da estação, chuva e ponto de orvalho. As demais
variáveis aparecem como lacunas. Há uma consulta de cobertura e no
máximo uma transferência CSV por hora. Quando o WIS2 acrescenta dados,
uma nova tentativa pode ampliar a cobertura publicada.

Os identificadores WIGOS da rede automática vêm do catálogo oficial
WIS2. O vínculo com os códigos usados na API foi feito por nome
normalizado e coordenadas a até 100 m no ZIP oficial de 2026. Vínculos
ambíguos são descartados. Para atualizar o mapa, use um ZIP anual oficial
e execute na raiz do repositório:

```bash
python cloudflare/scripts/map-wis2-stations.py data/2026.zip
```

[Catálogo WIS2 de estações](https://wis2bra.inmet.gov.br/oapi/collections/stations) · [Observações automáticas no WIS2](https://wis2bra.inmet.gov.br/oapi/collections/urn:wmo:md:br-inmet:synop) · [Arquivo oficial do INMET](https://portal.inmet.gov.br/dadoshistoricos)

[Limites de Workers](https://developers.cloudflare.com/workers/platform/limits/) · [Limites de Workflows](https://developers.cloudflare.com/workflows/reference/limits/) · [Limites de KV](https://developers.cloudflare.com/kv/platform/limits/)

## Verificação do protocolo

`npm run test:workflow` usa Workerd e fontes simuladas para exercitar o
Cron, o formulário, a confirmação, o download, a indexação e a consulta.
O ensaio provoca falhas transitórias e verifica a recuperação sem pedidos
duplicados. Ele não envia solicitações reais ao INMET nem publica Workers.

## Publicação e acompanhamento

No diretório `cloudflare/`, use Node.js 22 ou superior e a versão
fixada do Wrangler. Os arquivos de exemplo incluem os nomes lógicos dos
recursos que cada instalador configura na própria conta.

```bash
npm ci
npm test
npm run test:workflow
npm run build
npx wrangler whoami
```

Copie as configurações para arquivos locais ignorados pelo Git:

```powershell
Copy-Item wrangler.jsonc wrangler.local.jsonc
Copy-Item bdmep.wrangler.jsonc bdmep.wrangler.local.jsonc
npx wrangler kv namespace create INMET_DAYS
```

Preencha o mesmo ID de KV nas duas cópias. Ajuste nelas os nomes de Worker
e Workflow, incluindo a referência `script_name`, se os nomes de exemplo
já estiverem ocupados na conta. Em Bash, use `cp` no lugar de `Copy-Item`.

O Worker principal usa o binding de navegador `INMET_BROWSER` para obter
as tabelas. Essa integração usa as permissões do próprio Worker. Confira
o tempo de Browser Run disponível na conta. [Preços e limites do Browser Run](https://developers.cloudflare.com/browser-run/pricing/)

No coletor BDMEP, configure em `vars` da cópia local o endereço exclusivo
`BDMEP_ADDRESS` e a conta `BDMEP_BROWSER_ACCOUNT`. Roteie esse endereço
para o coletor usando Cloudflare Email Routing. A credencial exclusiva de
Browser Run fica como segredo do Worker; informe seu valor somente no
prompt do Wrangler ou no painel Cloudflare.

```bash
npx wrangler deploy --config bdmep.wrangler.local.jsonc
npx wrangler secret put BDMEP_BROWSER_TOKEN --config bdmep.wrangler.local.jsonc
npx wrangler deploy --config wrangler.local.jsonc
```

Reutilize as cópias locais nos próximos deploys. O gatilho do Worker de
consulta chama o Workflow do BDMEP por binding. Os exemplos de consulta
neste guia usam o servidor local; na publicação, substitua a origem pelo
endereço informado pelo Wrangler.

Confira `/health`, a leitura de um dia com dados e o Cron `0 * * * *`
depois da publicação. No painel de Workflows, a janela UTC aparece no fim
do identificador. Por exemplo, `bdmep-janela-2026-11-26T14` corresponde
à janela das 11h de Brasília de 26/11. O parâmetro `day` informa o
dia solicitado. O resultado `indexed`, com a quantidade de estações
e o tamanho em bytes, confirma que o lote chegou ao índice.

Para diagnosticar o BDMEP, abra a instância do Workflow `nuvem-inmet-bdmep`
e veja `diagnostics` no resultado da etapa do formulário. Ali ficam
as fases em ordem, o tempo decorrido e os códigos HTTP que o navegador
conseguiu ler. Um código vazio indica que o navegador não expôs o status.
Uma resposta HTTP 4xx ou 5xx encerra a tentativa antes de interagir com a página.
O Worker também emite eventos `bdmep_coleta` com a fase e a instância em
`instance`. Os eventos omitem endereços, credenciais e links privados do pedido.

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
