# Formato binário Nuvem-INMET, versão 1

Todos os campos binários usam ordem de bytes little-endian, sem alinhamento ou
preenchimento. O catálogo SQLite aponta para arquivos binários imutáveis e
registra o SHA-256 de cada conteúdo. O hash cobre todos os bytes, inclusive o
cabeçalho e as extensões. A verificação integral é independente da leitura
pontual, que busca apenas os campos necessários. Os offsets começam em zero.

## Cadastro de estações

Cada arquivo `NIMS` descreve uma versão das coordenadas de uma estação. A data
de coleta, a fonte e o histórico ficam no catálogo.

| Offset | Campo | Tipo |
| --- | --- | --- |
| 0 | Assinatura `NIMS` | 4 bytes ASCII |
| 4 | Versão `1` | UInt8 |
| 5 | Identificador interno positivo | UInt32 |
| 9 | Latitude em graus | Float64 IEEE 754 |
| 17 | Longitude em graus | Float64 IEEE 754 |
| 25 | Comprimento do código original | UInt16 |
| 27 | Código original | UTF-8, sem terminador |

Cada coordenada pode estar ausente de modo independente. Um campo ausente usa
o padrão Float64 quiet NaN `0x7ff8000000000000`, escrito em little-endian como
`00 00 00 00 00 00 f8 7f`. Outros padrões NaN, infinitos e valores fora dos
limites geográficos são inválidos.

## Bloco horário

Cada arquivo `NIMO` abrange uma estação e um ano civil em UTC.

| Offset | Campo | Tipo |
| --- | --- | --- |
| 0 | Assinatura `NIMO` | 4 bytes ASCII |
| 4 | Versão `1` | UInt8 |
| 5 | Identificador interno da estação | UInt32 |
| 9 | Início do ano às 00:00 UTC em segundos Unix | Int64 |
| 17 | Quantidade de horas no ano | UInt32 |
| 21 | Quantidade de entradas de precisão estendida | UInt32 |
| 25 | Sequência de registros horários | 3 bytes por hora |

A hora de índice `i` começa em `início + 3600 × i` e ocupa os três bytes a
partir do offset `25 + 3 × i`. Anos comuns têm 8.760 horas e anos bissextos
têm 8.784. Com os 25 bytes do cabeçalho e sem extensão, o arquivo usa 26.305
ou 26.377 bytes, respectivamente.

| Variável | Tipo | Valor direto | Ausência | Precisão estendida |
| --- | --- | --- | --- | --- |
| Temperatura | Int16 | Inteiro de −32.766 a 32.767 dividido por 10, em °C | −32.768 | −32.767 |
| Umidade relativa | UInt8 | Inteiro de 0 a 100, em % | 255 | 254 |

A combinação `(−32768, 255)` indica uma hora sem linha na fonte. A combinação
`(−32768, 253)` indica uma linha presente com ambos os campos nulos. Os demais
códigos de umidade são inválidos. Cada componente também permite ausência
individual. Temperatura e umidade iguais a zero são valores disponíveis.

### Precisão estendida

O codificador verifica cada valor recebido antes de usar uma escala inteira.
Valores finitos representáveis na escala e no intervalo usam a posição de
três bytes. Temperaturas fora da escala ou da faixa compacta e umidades
fracionárias usam uma seção de extensão sem perda de valor numérico. Valores
de umidade fora de 0 a 100 e números `NaN` ou infinitos são inválidos.

Após o último registro horário, seguem as entradas do índice estendido em
ordem crescente de hora. Cada entrada tem 16 bytes e contém, nesta ordem, o
índice da hora (UInt32), o offset absoluto do conteúdo (UInt64) e seu
comprimento (UInt32). Após o índice, os conteúdos são gravados na mesma
ordem. Cada conteúdo tem dois campos decimais ASCII separados por um byte
nulo, primeiro temperatura e depois umidade. O campo fica vazio quando
a variável correspondente usa o valor de três bytes ou é nula. Um leitor
localiza o conteúdo de uma hora marcada com busca binária no índice.

A escala compacta preserva o valor numérico. Zeros à direita e o sinal de
zero seguem a forma canônica da escala. Uma leitura devolve números
decimais e distingue uma hora ausente de uma observação com campos ausentes.
