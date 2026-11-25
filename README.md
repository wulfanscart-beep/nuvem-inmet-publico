# Nuvem-INMET

Acervo local de observações meteorológicas horárias. Armazena temperatura do ar
em °C e umidade relativa em %, com consulta offline e exportação de recortes.
A coleta padrão usa a API pública do INMET. Também é possível importar ZIPs
históricos oficiais já baixados e, opcionalmente, consultar a Base dos Dados
no BigQuery.

## Instalação

Requer Python 3.11 ou superior. A coleta pelo INMET, a importação de ZIPs e as
consultas locais usam a biblioteca padrão do Python.
O armazenamento local foi testado em Linux. A sincronização de arquivos
e diretórios do acervo Python pode falhar no Windows com `Bad file descriptor`.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

O acervo fica em `./data` por padrão. Use `NUVEM_INMET_DATA` ou
`nuvem-inmet --data-dir CAMINHO ...` para mudar o diretório.

## Brasil inteiro com atualização diária

O programa `run` inicia por padrão em 1º de janeiro de 2020, coleta
a última data UTC completa e continua preenchendo as lacunas históricas
em lotes de até 31 dias por ciclo. Enquanto o processo estiver em execução,
a próxima atualização acontece no horário configurado. O padrão é 08:00 UTC
(05:00 em Brasília). Se a coleta falhar, o programa conserva os dados já
gravados e repete a tentativa em 15 minutos.

```bash
nuvem-inmet run --at 08:00
```

A API pública fornece uma consulta nacional por data para as estações
automáticas. A cobertura histórica dessa rota depende das datas que o INMET disponibiliza. Para carregar anos históricos em um ciclo,
informe os ZIPs anuais do INMET. O importador inclui todas as estações
presentes, com cobertura independente por estação. Para convencionais,
informe os CSVs horários do BDMEP em `--bdmep-csv` ou configure uma URL
HTTPS estável e renovada em `INMET_BDMEP_CSV_URL`. O programa mantém os
códigos convencionais e automáticos separados. [ZIPs anuais do INMET](https://portal.inmet.gov.br/dadoshistoricos) · [BDMEP](https://bdmep.inmet.gov.br/)

```bash
# Carrega arquivos existentes sem usar a rede, valida o ciclo e encerra.
nuvem-inmet run --offline --once --archive /arquivos/2020.zip \
  --archive /arquivos/2021.zip --bdmep-csv /arquivos/convencionais.csv

# Usa os ZIPs como carga inicial e acompanha a API diariamente.
nuvem-inmet run --archive /arquivos/2025.zip --history-days 0
```

Para iniciar pelo espelho nacional fixo, instale o extra `parquet`.
Ele inclui séries automáticas e convencionais até dezembro de 2025.
O programa faz uma única projeção em quatro colunas, valida a versão
do arquivo e grava no acervo apenas datas desde 2020. Os grupos do
Parquet misturam anos e a projeção nacional transfere cerca de 686 MB.
Durante a primeira carga, o programa guarda os recortes validados no
diretório temporário do acervo, reutiliza-os se o processo reiniciar e
libera esse espaço após a publicação dos blocos binários.
[Espelho nacional no Kaggle](https://www.kaggle.com/datasets/mauricioelandim/brazil-rain-data-inmet)

```bash
python -m pip install -e '.[parquet]'
nuvem-inmet run --mirror-history
```

O catálogo registra os intervalos concluídos e a rotina retoma a coleta
após reiniciar. O acervo guarda temperatura e umidade nos blocos binários
por estação e ano. O CSV em `data/relatorio-brasil.csv` é uma saída
derivada. Mantenha o diretório do acervo em disco persistente e o processo
rodando para as atualizações ocorrerem diariamente.

### Relatório mensal

O relatório usa leituras simultâneas de 10h a 21h UTC e temperaturas de 24 a
50 °C para as duas médias. Para a máxima, usa a maior temperatura registrada
no horário de avaliação e a umidade da mesma leitura. Em empate, seleciona a
primeira ocorrência. As duas colunas de insuflamento ficam vazias até que
a matriz de referência esteja disponível. O relatório nacional omite
meses sem valores e usa uma linha por estação e mês.

```bash
nuvem-inmet report-monthly --start 2020-01-01 --end 2026-01-01 > brasil.csv
nuvem-inmet report-monthly 83222 --start 2020-01-01 --end 2026-01-01 > cruz-das-almas.csv
```

### Recorte opcional de Cruz das Almas anterior a 2020

Este comando independente carrega o espelho fixo com 38.001 linhas reais
da estação convencional 83222 de 1961 a 2019, incluindo 33.600 pares
de temperatura e umidade da mesma leitura. O acervo mantém as lacunas.
[Espelho histórico do INMET, versão 1, Saraiva, CC BY 4.0](https://www.kaggle.com/datasets/saraivaufc/conventional-weather-stations-brazil)

```bash
nuvem-inmet run --station 83222 --start 1961-01-01 --once
```

## Coleta gratuita pelo INMET

A primeira execução usa uma pequena amostra de estações automáticas do Paraná.
Repita `--station CÓDIGO` para escolher outras estações. Datas sem horário
começam à meia-noite UTC. O fim de cada intervalo é exclusivo.

```bash
nuvem-inmet sync historical --start 2024-02-28 --end 2024-03-02
nuvem-inmet stations
nuvem-inmet status
```

Use `incremental` com o intervalo desejado para buscar apenas as janelas de
observações ainda não consultadas com sucesso. Use `reconcile` para consultar
novamente uma janela e incorporar linhas novas ou valores revisados. Uma
omissão da fonte preserva o valor armazenado. Uma falha na coleta mantém o
último acervo válido disponível para consulta.

```bash
nuvem-inmet sync incremental --start 2024-02-28 --end 2024-03-03
nuvem-inmet sync reconcile --start 2024-02-29 --end 2024-03-01
```

O acesso à API requer saída HTTPS para `apitempo.inmet.gov.br`. Em ambientes
com acesso de rede restrito, autorize esse domínio ou importe um ZIP local.
O catálogo de estações da API fornece coordenadas de cadastro, cuja vigência
histórica pode ser desconhecida. [API pública do INMET](https://apitempo.inmet.gov.br/)

## Importação de ZIP oficial

Obtenha o ZIP anual na página de dados históricos do INMET e indique o arquivo
local. Repita `--archive` para intervalos que abrangem mais de um ano. O leitor
abre diretamente no ZIP os CSVs das estações selecionadas e importa somente
o intervalo solicitado. [Dados históricos do INMET](https://portal.inmet.gov.br/dadoshistoricos)

```bash
nuvem-inmet sync historical --source archive --archive ~/Downloads/2024.zip \
  --start 2024-02-28 --end 2024-03-02
```

## Consultas offline

Use em lugar de `A807` um código que aparece em `stations`.

```bash
nuvem-inmet point A807 2024-02-29T12:00Z
nuvem-inmet gaps A807 --start 2024-02-28 --end 2024-03-02
nuvem-inmet export A807 --start 2024-02-28 --end 2024-03-02 --format csv > recorte.csv
nuvem-inmet status --station A807
nuvem-inmet verify
```

Na biblioteca, `observation()` devolve `None` para uma hora sem linha na
fonte. Uma linha publicada com valores ausentes devolve uma `Observation`
com os campos correspondentes nulos. As exportações também incluem
`recorded` para distinguir os casos. O JSON usa strings decimais para
preservar a precisão.

```python
from datetime import datetime
from nuvem_inmet.query import Query

archive = Query("data")
start = datetime.fromisoformat("2024-02-29T00:00Z")
end = datetime.fromisoformat("2024-03-01T00:00Z")

print(archive.stations())
print(archive.nearest(-25.43, -49.27, limit=3))
print(archive.observation("A807", start))
print(archive.provenance("A807", start))
for slot in archive.series("A807", start, end):
    print(slot.timestamp, slot.observation)
```

## BigQuery opcional

Instale `.[bigquery]` apenas se quiser usar a Base dos Dados. Esse adaptador
requer Application Default Credentials do Google Cloud e um projeto com
permissão para criar jobs. Configure as credenciais pelo ambiente do Google
Cloud ou pelo `gcloud auth application-default login`. [Base dos Dados](https://basedosdados.org/dataset/782c5607-9f69-4e12-b0d5-aa0f1a7a94e2?table=2c7fdc3d-f2ed-4c78-84b8-d9c792a06703)

```bash
python -m pip install -e '.[bigquery]'
nuvem-inmet sync historical --source bigquery --project MEU_PROJETO \
  --start 2020-02-28 --end 2020-03-02 --max-bytes 100000000
```

O comando acima executa apenas estimativas via dry run. Para autorizar
consultas faturáveis, informe `--allow-billed-queries` após conferir a
estimativa. O teto de bytes é cumulativo e inclui cadastro e observações.
Para aplicar também um teto monetário, passe `--max-cost` e
`--price-per-tib` na mesma moeda, usando o preço da sua região. Os limites
cobrem bytes de consultas sob demanda. [Preços do BigQuery](https://cloud.google.com/bigquery/pricing)

## Nuvem-INMET no Cloudflare

A API pública consulta uma estação e um dia em JSON ou CSV. É possível
selecionar temperatura, umidade, vento, chuva, pressão, radiação e
ponto de orvalho, incluindo as máximas e mínimas horárias disponíveis.
O Worker coleta diariamente às 05h de Brasília e publica um lote por dia
no Workers KV. [API e operação](cloudflare/README.md)

## Documentação

- [Formato binário e códigos de ausência](docs/formato-binario.md)
- [Arquitetura, fontes e integridade](docs/arquitetura.md)
- [Benchmark e comparação com CSV, JSON e Parquet](docs/desempenho.md)

## Licença

O código e a documentação do Nuvem-INMET são distribuídos sob a licença MIT. Ela
permite usar, adaptar e redistribuir o código, inclusive para fins comerciais.
Preserve o aviso de direitos autorais e o texto da licença nas cópias ou em
partes substanciais do código. [Texto da licença](LICENSE)

Os dados do INMET, os conjuntos de dados de terceiros e as dependências
seguem os termos das respectivas fontes e projetos.
