# Desempenho do armazenamento

## Reprodução

Em um ambiente virtual Linux, execute na raiz do projeto.

```bash
python -m pip install -e '.[benchmark]'
python benchmarks/medir.py --stations 8 --year 2024 --lookups 250 --repeats 3
```

O benchmark gera séries sintéticas com semente fixa, temperatura em décimos de grau Celsius, umidade relativa em pontos percentuais inteiros, cerca de 3% de horários sem registro e campos ausentes em parte dos horários registrados. Cada formato usa um arquivo por estação e ano. CSV e JSON contêm o horário UTC de cada linha. O Parquet usa timestamps UTC, temperatura decimal, umidade de 8 bits e compressão Zstandard, com grupos de 720 linhas. No binário o horário deriva da posição. As lacunas do cenário são lidas como dois campos vazios em todos os formatos.

As consultas pontuais escolhem os mesmos pares de estação e hora por sorteio reproduzível. Cada consulta abre o arquivo e recupera a observação. O CSV percorre as linhas até o índice, o JSON decodifica o arquivo, o Parquet lê o grupo de linhas correspondente e o binário acessa o deslocamento calculado. A carga de séries lê os arquivos completos e percorre os campos de temperatura e umidade. O benchmark confere a quantidade de horários e compara o SHA-256 dos valores decodificados entre formatos e repetições.

A medição usa a mediana de três repetições em processos separados. O pico RSS é o maior pico de memória residente dos três processos por carga, incluindo o interpretador e as bibliotecas. Os arquivos são gravados antes da leitura e o cache de disco do sistema operacional pode ser usado. Os tempos cobrem a leitura, a decodificação e a verificação dos valores. Os tamanhos são os bytes lógicos dos arquivos de observações. Essas medições excluem o catálogo SQLite, arquivos de estações, revisões históricas e a verificação integral de integridade dos arquivos.

## Resultado medido

Execução de referência em Linux x86-64 com Python 3.12.14 e PyArrow 24.0.0. O cenário tem 8 estações, 8.784 horários por estação no ano bissexto de 2024 e 250 consultas pontuais por repetição.

| Formato | Bytes | Ponto (ms/consulta) | Pico RSS ponto (MiB) | Séries completas (s) | Pico RSS séries (MiB) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Binário | 211.016 | 0,0157 | 14,1 | 0,0808 | 14,1 |
| CSV | 2.062.009 | 1,4055 | 14,1 | 0,0916 | 14,1 |
| JSON | 5.827.345 | 5,0311 | 19,1 | 0,0889 | 19,1 |
| Parquet | 617.273 | 0,2882 | 61,7 | 0,1059 | 61,9 |

O binário ocupou 10,2% do tamanho do CSV, 3,6% do JSON e 34,2% do Parquet nessa configuração. Cada arquivo binário tem 26.377 bytes, com 26.352 bytes para horários e 25 bytes de cabeçalho. A maior diferença de tempo ocorreu nas consultas pontuais. Os tempos da leitura integral ficaram entre 81 ms e 106 ms. Os resultados dependem da densidade de registros, da precisão numérica, do cache e dos grupos de linhas do Parquet. A medição usa a codificação compacta comum e fica como referência para avaliar arquivos reais com extensões de precisão. A compressão opcional do binário por blocos ficou fora desta medição.
