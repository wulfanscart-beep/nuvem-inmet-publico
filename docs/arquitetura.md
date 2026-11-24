# Arquitetura

| Componente | Responsabilidade |
| --- | --- |
| `src/collectors` | Adapta a API pública do INMET, os arquivos históricos oficiais e a Base dos Dados no BigQuery. |
| `src/validation` | Valida horários, estações, coordenadas e valores. Normaliza observações para horas UTC e números decimais. |
| `src/codec` | Codifica e lê blocos estação-ano e versões binárias do cadastro. |
| `src/storage` | Gerencia o catálogo SQLite, a cobertura das coletas, os checksums e as revisões. |
| `src/query` | Lê o catálogo e os blocos locais para pontos, séries, lacunas, procedência e exportações. |

Um coletor implementa `source`, `stations(codes)` e
`observations(codes, start, end)`. A sincronização trabalha com os modelos
`Station` e `Observation`, comuns a todos os adaptadores, e usa janelas
semiabertas em UTC. O consumidor depende apenas do acervo local.

## Precisão, relógio e coordenadas

Temperaturas e umidades são mantidas como valores decimais após a
normalização. Arquivos textuais do INMET são interpretados sem arredondamento.
Na Base dos Dados, ambas as variáveis são `FLOAT64`. O processamento
publicado converte os campos originais para números sem fixar uma escala.
Nesse caso preservamos o menor decimal que reconstrói o mesmo `FLOAT64`
retornado pelo SDK. A precisão efetivamente observada em consultas ao
BigQuery depende do recorte. O codificador testa a representação exata em
cada ponto, inclusive quando o decimal tem mais dígitos do que o formato
compacto comporta. [Transformação na Base dos Dados](https://github.com/basedosdados/mais/blob/master/bases/br_inmet_bdmep/code/func/get_clima_info.R)

O índice de cada registro é o número inteiro de horas após 1º de janeiro à
meia-noite UTC. Um bloco anual completo custa 26.305 bytes em um ano comum
ou 26.377 em um ano bissexto, antes de extensões e catálogo. Temperaturas
negativas ou fora da faixa compacta são representadas sem descarte
arbitrário. Lacunas, campos nulos e zeros têm representações distintas.

Cadastro e observações mantêm o código original da estação. Um cadastro sem
data de vigência explícita fica associado à data de coleta no catálogo
e pode ser exibido na consulta de cadastro atual. Uma consulta de posição
em data específica exige uma versão com vigência conhecida. Versões
anteriores são preservadas quando a posição muda. O cálculo de distância
usa a posição conhecida no momento pedido.

## Sincronização e integridade

Cada tentativa registra fonte, modo, janela e horário de execução. A
cobertura da tentativa bem-sucedida é registrada por estação para evitar
repetir consultas, inclusive quando a fonte não publicou uma linha em
determinada hora. `status` distingue a última tentativa, a última coleta
com sucesso e a hora da última linha efetivamente armazenada.

Registros iguais repetidos dentro da tentativa são contados uma vez.
Duplicatas com valores divergentes e alterações conflitantes de fontes
diferentes abortam a atualização. A reconciliação pode revisar um valor
da mesma fonte ou preencher uma lacuna com uma linha publicada. Uma
omissão ambígua da origem preserva a linha já armazenada. A identificação
da tentativa que introduziu o valor fica disponível por hora.

Os blocos publicados são imutáveis. O gravador promove arquivos completos
por renomeação atômica e só publica as novas referências quando confirma a
transação SQLite. Uma falha antes da confirmação mantém os leitores no
estado anterior. O catálogo retém referências para versões anteriores e
`verify` recalcula SHA-256 dos arquivos cadastrados. Uma parada abrupta
pode deixar arquivos completos sem referência, que ficam fora do acervo
visível. Ao preservar ou restaurar um acervo, mantenha o catálogo e o
diretório de blocos e cadastros juntos.

A consulta pontual calcula o deslocamento dentro do arquivo, e a série
percorre as posições em ordem. A verificação integral de checksums tem
um comando próprio, para que a leitura pontual acesse apenas os bytes
necessários. O acervo mantém revisões para auditoria, o que aumenta o
espaço em disco após atualizações.

## Validação

Os testes exercitam a sincronização e a leitura offline com amostras pequenas
e identificadas como sintéticas, além dos adaptadores com respostas
simuladas. A [comparação de desempenho](desempenho.md) documenta o cenário
medido, os tempos, a memória e o custo de armazenamento.

Também foi validada a importação de três arquivos históricos de 2024,
copiados de um espelho público dos CSVs do INMET. O recorte de 28 de
fevereiro a 2 de março, com fim exclusivo, contém 216 linhas de A807
(Curitiba), A818 (Ivaí) e A819 (Castro). São 144 linhas com valores
disponíveis, cujas temperaturas usam até uma casa decimal e cujas
umidades são inteiras, e 72 linhas publicadas com ambos os valores
ausentes. Os três arquivos binários somaram 79.131 bytes para o ano
bissexto, sem extensão de precisão. O recorte confirma a escala para
essa amostra. A regra de precisão estendida segue válida para outros
períodos e fontes. [CSV das estações usado na validação](https://github.com/lucasfrag/climate-data-storytelling/tree/f9df55cc54e5a1bfe2ff41ade4fc2ede324afcab/dataset/2024)

## Carga nacional desde 2020

O programa executa a carga inicial e os ciclos diários em primeiro plano.
Para a API pública do INMET, faz uma requisição nacional por dia UTC e
registra intervalos já concluídos. A consulta das automáticas e os arquivos
convencionais do BDMEP mantêm identidades separadas. Um ZIP anual contribui
com todas as estações e intervalos presentes no arquivo, inclusive quando
uma estação começa a operar no meio do ano.

Como fonte histórica opcional, a projeção do espelho Parquet usa versão e
intervalos HTTP fixos. Lê código, carimbo de tempo, temperatura de bulbo
seco e umidade relativa, além das coordenadas para o cadastro. Seu
catálogo informa UTC−3, mas os horários gravados coincidem com a hora UTC
do CSV original nos 8.600 pares testados da automática A807 em 2024 e
em leituras da convencional 83222. O adaptador interpreta a versão
fixada conforme essa verificação (UTC). Os arquivos dessa versão
terminam em 2025. [Espelho nacional, versão 1](https://www.kaggle.com/datasets/mauricioelandim/brazil-rain-data-inmet)

A compactação de três bytes por observação vale para o bloco principal
quando a precisão cabe na escala comum. O total em disco inclui catálogo,
estações e versões de blocos preservadas para auditoria.
