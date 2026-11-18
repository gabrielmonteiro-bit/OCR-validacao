# Extração das faturas em CSV (Document AI)

    pip install -r requirements.txt
    python extrair.py              # processa documentos/ e gera saida/
    python extrair.py --sem-api    # regenera os CSVs a partir do respostas.json
    python extrair.py --reprocessar

saida/<fatura>.csv: uma linha por valor retornado pelo modelo.
saida/todas_faturas.csv: todas as faturas juntas.

Colunas: arquivo; distribuidora; campo; subcampo; ocorrencia; valor_extraido;
valor_normalizado; confianca; pagina; posicao

- Tabelas: ocorrencia = número da linha da tabela (mês do histórico, item da fatura).
- Campo simples com ocorrencia 2, 3...: o modelo retornou mais de um valor para o campo.
- Campo do schema não retornado: linha com valor vazio.
- Schema ativo editável em SCHEMA no extrair.py (history_days está fora por estar inativo).

Configuração: GOOGLE_APPLICATION_CREDENTIALS, DOCAI_PROJECT, DOCAI_LOCATION,
DOCAI_PROCESSOR e DOCAI_VERSION (ID da versão treinada que vai para o sistema).

A pasta saida/ do zip é um exemplo gerado com respostas antigas: nelas faltavam página,
posição, valor normalizado e distribuidora das faturas escaneadas. Na execução real essas colunas vêm preenchidas.
