# Residencial Aurora — assistente virtual

Assistente virtual do condomínio Residencial Aurora, construído com Google ADK e exposto por uma API HTTP em Python/FastAPI. Permite consultar o regulamento, reservar e cancelar áreas comuns e autorizar visitantes. As operações que geram cobrança ou liberam acesso dependem de confirmação explícita pela API.

## Arquitetura

A aplicação usa um agente principal do Google ADK, que encaminha as solicitações para especialistas:

| Agente | Responsabilidade |
|---|---|
| Principal | Recebe a mensagem e encaminha a solicitação ao especialista adequado. |
| Reservas | Consulta disponibilidade, cria reservas e cancela reservas do apartamento associado à sessão. |
| Visitantes | Solicita autorização de entrada de visitantes. |
| Regulamento | Consulta `dados/regulamento.md` para responder dúvidas sobre as regras do condomínio. |

As tools de negócio consultam e alteram os dados persistidos. O apartamento usado pelas tools vem do estado da sessão, não de um valor escolhido livremente pelo modelo.

A API oferece rotas para conversa, confirmação de ações, consulta de eventos e verificação direta de reservas e visitantes.

## Garantias

Cada garantia abaixo aponta os pontos de implementação e os testes correspondentes. As decisões críticas são verificadas pela API, pelas ferramentas e pelo banco de dados; não dependem somente de uma instrução ao modelo.

### Garantia 1 — cobrança ou acesso só com confirmação explícita

- `src/aurora/tools.py` — a ferramenta `reservar_area` é registrada com `require_confirmation_reservar_area`, que exige confirmação quando a área tem taxa; `autorizar_visitante` é registrada com `require_confirmation=True`, portanto sempre exige confirmação.
- `src/aurora/confirmacao.py::responder_confirmacao` — processa a resposta à confirmação e verifica o estado da pendência antes de executar a ação.
- `src/aurora/persistencia.py` — mantém o registro de confirmações e suas transições de estado.

A mensagem do morador ou uma afirmação do modelo como “já confirmei” não substitui a chamada à rota de confirmação. A confirmação precisa corresponder a uma pendência válida daquela sessão; IDs inexistentes ou já respondidos retornam HTTP 409. A aprovação executa a ação uma única vez, enquanto a recusa não grava a reserva ou o visitante.

A cobrança é determinada pela área oficial, não por uma suposição do modelo: `src/aurora/persistencia.py::resolver_area` resolve a área usando os dados do condomínio; uma área não resolvida não deve ser tratada como gratuita. Os testes de `tests/integracao/test_resolucao_area.py` cobrem a resolução de nomes e IDs de áreas.

### Garantia 2 — cada sessão pertence a um apartamento

- `src/aurora/runtime.py::criar_sessao` — grava o apartamento no estado da sessão (`state["apartamento"]`) quando a sessão é criada.
- `src/aurora/tools.py` — as ferramentas de reservas e visitantes obtêm o apartamento de `tool_context.state["apartamento"]`, em vez de aceitar um apartamento escolhido como argumento pelo modelo.
- `tests/integracao/test_topologia.py::test_schema_das_tools_nao_tem_parametro_de_apartamento` — verifica que as ferramentas não expõem um parâmetro `apartamento` ao modelo.
- `src/aurora/persistencia.py` — as operações de negócio recebem o apartamento resolvido pela aplicação e filtram as alterações por esse apartamento.

Assim, o modelo pode interpretar o pedido, mas não escolhe livremente qual apartamento consultar ou modificar por meio dos argumentos da ferramenta. Os testes manuais também verificaram recusas a tentativas de consultar visitantes ou cadastrar visitantes em outros apartamentos; esses cenários não constituem uma auditoria de segurança exaustiva.

### Garantia 3 — persistência após reinício

- `src/aurora/runtime.py` — configura o `DatabaseSessionService` com SQLite para persistir sessões e eventos do ADK em `data/adk.db`.
- `src/aurora/persistencia.py` — persiste reservas, visitantes, confirmações e dados de negócio em `data/app.db`.
- `src/aurora/subir.py` — inicializa o esquema e os dados-base do condomínio.
- `src/aurora/restaurar.py` — repõe as reservas e os visitantes iniciais a partir de `dados/`; esse comando exige a API parada e não é necessário a cada reinício normal.
- `tests/reinicio/test_reinicio_real_api.py` — cobre o reinício real da API.

Os dados não dependem apenas da memória do processo ou do histórico mantido em RAM. Na verificação manual, após parar e iniciar novamente a API sem executar a restauração, as reservas e os visitantes cadastrados permaneceram disponíveis e a sessão anterior continuou acessível pela rota de eventos.

### Garantia 4 — o regulamento é consultado sob demanda

- `src/aurora/tools.py::criar_tools_regulamento` — registra a ferramenta `consultar_regulamento`, que retorna artigos relevantes em vez de incluir todo o regulamento em cada solicitação.
- `src/aurora/agentes.py::INSTRUCAO_PRINCIPAL` — as instruções do agente principal não incluem o texto do regulamento.
- `tests/integracao/test_regulamento.py` — verifica a consulta por número e termos, o limite de artigos retornados e os eventos da consulta.
- `tests/integracao/test_topologia.py::test_instrucoes_do_principal_nao_contem_texto_do_regulamento` — verifica que o regulamento não está nas instruções do agente principal.

O texto é recuperado pela ferramenta do especialista em regulamento quando necessário. Isso reduz a dependência de informações normativas inseridas no prompt principal e evita adicionar o regulamento completo a consultas que não precisam dele.

### Garantia 5 — dois moradores, uma reserva ativa para a mesma área e data

- `src/aurora/persistencia.py` — o índice único parcial `ux_reservas_ativa` aplica unicidade a `reservas(area, data)` somente para registros com `status = 'ativa'`.
- `src/aurora/tools.py` — a ferramenta de reserva trata a violação de integridade do SQLite como um resultado de negócio, em vez de depender somente de uma checagem prévia do modelo.
- `tests/concorrencia/test_concorrencia_http.py::test_duas_aprovacoes_simultaneas_do_mesmo_id_uma_200_uma_409` — verifica duas aprovações concorrentes do mesmo ID.
- `tests/concorrencia/test_concorrencia_http.py::test_duas_aprovacoes_simultaneas_ids_diferentes_sessoes_diferentes_uma_reserva` — verifica sessões e IDs diferentes disputando a mesma área/data.

A exclusividade é aplicada pelo SQLite no momento da gravação, inclusive quando as requisições chegam simultaneamente. Na segunda situação concorrente, os testes verificam que apenas uma reserva é efetivada e que a API não responde com erro HTTP 500.

Essas garantias são sustentadas pela implementação e pelos testes indicados. Os resultados de teste registrados neste README correspondem às execuções descritas na seção [Verificações adicionais](#verificações-adicionais), não a uma nova execução integral feita durante a edição deste documento.

## Como instalar e executar

### 1. Pré-requisitos

- Python 3.12 ou superior.
- [`uv`](https://docs.astral.sh/uv/).
- Chave do [Google AI Studio](https://aistudio.google.com/apikey) para executar a API com o modelo Gemini real.

### 2. Configurar a chave

Na raiz do repositório, execute:

```bash
cp .env.example .env
```

Edite `.env` e preencha sua própria chave. Mantenha `GOOGLE_GENAI_USE_VERTEXAI` vazia ou ausente para usar o Google AI Studio diretamente, conforme o `.env.example`:

```dotenv
GOOGLE_API_KEY=sua_chave_aqui
```

Não compartilhe nem versione `.env` ou uma chave real.

### 3. Instalar as dependências

```bash
uv sync --extra test
```

### 4. Restaurar os dados iniciais

**A API precisa estar parada para restaurar os dados.** Execute:

```bash
uv run python -m aurora.restaurar
```

O comando restaura as reservas e os visitantes a partir dos arquivos em `dados/`. Execute-o com a API parada; ele repõe os dados iniciais e não deve ser usado durante a execução normal para preservar os dados de teste.

### 5. Subir a API

```bash
uv run python -m aurora.subir
```

A API ficará disponível em `http://localhost:8000`. Para parar, use `Ctrl+C`.

A inicialização prepara o esquema e os dados-base do condomínio. A restauração é o passo que repõe as reservas e os visitantes iniciais.

## Rotas HTTP utilizadas

| Método e rota | Finalidade |
|---|---|
| `POST /sessoes` | Cria uma sessão para um apartamento. Exemplo: `{"apartamento":"101"}`. |
| `POST /sessoes/{session_id}/mensagens` | Envia uma mensagem ao assistente. Exemplo: `{"texto":"Quais são as regras para utilizar a quadra?"}`. |
| `POST /sessoes/{session_id}/confirmacoes` | Aprova ou recusa uma ação pendente. Exemplo: `{"id":"ID_DA_CONFIRMACAO","confirmado":true}`. |
| `GET /sessoes/{session_id}/eventos` | Consulta os eventos da sessão. |
| `GET /apartamentos/{apartamento}/reservas` | Consulta as reservas do apartamento. |
| `GET /apartamentos/{apartamento}/visitantes` | Consulta os visitantes autorizados do apartamento. |

Quando uma ação exige confirmação, a resposta pode conter `resposta` vazia e uma lista `confirmacoes_pendentes`. Copie o `id` da pendência retornada e use-o na rota de confirmações.

## Testes automatizados

Com as dependências de teste instaladas, execute:

```bash
uv run pytest -q -m "not gemini_real"
```

**Resultado observado anteriormente no fork:** `136 passed, 1 skipped, 1 deselected`. Os testes direcionados executados posteriormente estão registrados na seção [Verificações adicionais](#verificações-adicionais).

Esse comando exclui os testes marcados `gemini_real`, que fazem chamadas reais ao Gemini. Durante a execução foram exibidos avisos do Starlette e do ADK, mas eles não impediram os testes de passar.

## Testes manuais — passo a passo executado

Os testes abaixo foram realizados com a API em execução, usando o Swagger em `http://127.0.0.1:8000/docs` para enviar as requisições e o terminal para conferir os dados persistidos. Os exemplos de `session_id` e IDs de confirmação abaixo são os usados durante essa execução; ao repetir os testes, use os IDs retornados pela sua própria API.

### 1. Subir a API e abrir o Swagger

Em um terminal, na raiz do projeto:

```bash
uv run python -m aurora.subir
```

Em seguida, abra `http://127.0.0.1:8000/docs`.

### 2. Criar uma sessão para o apartamento 101

No Swagger, execute `POST /sessoes` com:

```json
{
  "apartamento": "101"
}
```

A sessão usada nesta rodada foi:

```text
d1649eba-2a31-4067-8a53-96b91077384b
```

### 3. Consultar as regras da quadra

Execute `POST /sessoes/{session_id}/mensagens` com o ID da sessão criada e o corpo:

```json
{
  "texto": "Quais são as regras para utilizar a quadra do condomínio?"
}
```

**Resultado observado:** o assistente respondeu com informações do regulamento, incluindo o uso da quadra, reservas, responsabilidade por convidados, limpeza, ruído e proibições. `confirmacoes_pendentes` veio vazio.

### 4. Reservar a quadra sem confirmação pendente

Envie:

```json
{
  "texto": "Quero reservar a quadra para 20/04/2030."
}
```

**Resultado observado:** o assistente informou que a reserva foi criada e retornou o código `RSV-4822`, sem confirmação pendente.

Verifique no terminal:

```bash
curl -s http://localhost:8000/apartamentos/101/reservas
```

A resposta incluiu:

```json
{
  "codigo": "RSV-4822",
  "area": "quadra",
  "data": "2030-04-20"
}
```

A rota também manteve a reserva inicial `RSV-1377` da quadra em `2030-03-09`.

### 5. Solicitar e aprovar uma reserva paga

Envie:

```json
{
  "texto": "Quero reservar o salão de festas para 21/04/2030."
}
```

**Resultado observado:** a API retornou uma confirmação pendente com:

```json
{
  "id": "3f5d4a79-c9be-4595-887c-6538e30a8bba",
  "acao": "reservar_area",
  "detalhes": {
    "area": "salao-de-festas",
    "data": "2030-04-21"
  }
}
```

Aprove usando `POST /sessoes/{session_id}/confirmacoes`:

```json
{
  "id": "3f5d4a79-c9be-4595-887c-6538e30a8bba",
  "confirmado": true
}
```

**Resultado observado:** a API confirmou a reserva `RSV-4823`. A consulta de reservas do apartamento 101 mostrou a reserva do salão em `2030-04-21`.

### 6. Recusar uma reserva paga

Envie:

```json
{
  "texto": "Quero reservar o salão de festas para 22/04/2030."
}
```

A confirmação pendente retornada nesta execução foi:

```json
{
  "id": "4e33e600-70e4-4091-be99-e9f9894fc672",
  "acao": "reservar_area",
  "detalhes": {
    "area": "salao-de-festas",
    "data": "2030-04-22"
  }
}
```

Recuse:

```json
{
  "id": "4e33e600-70e4-4091-be99-e9f9894fc672",
  "confirmado": false
}
```

**Resultado observado:** a API limpou a lista de confirmações pendentes. Depois, `GET /apartamentos/101/reservas` continuou mostrando apenas as reservas `RSV-1377`, `RSV-4822` e `RSV-4823`; não foi criada reserva para `2030-04-22`.

### 7. Solicitar autorização de visitante usando “amanhã”

Envie:

```json
{
  "texto": "Quero autorizar a entrada do visitante João Silva para amanhã."
}
```

**Resultado observado:** foi criada uma confirmação pendente para `autorizar_visitante`, com nome `João Silva` e data `2025-02-16`.

A confirmação foi aprovada pela rota `POST /sessoes/{session_id}/confirmacoes`:

```json
{
  "id": "3fd63708-f886-4529-ad0d-e2648d89bb6e",
  "confirmado": true
}
```

A resposta informou o cadastro. A verificação:

```bash
curl -s http://localhost:8000/apartamentos/101/visitantes
```

retornou João Silva com a data `2025-02-16`.

**Resultado do teste:** a confirmação e a persistência funcionaram, mas a data não era a esperada: “amanhã” foi interpretado como uma data passada. O comportamento foi registrado como problema, não como sucesso da interpretação de data.

### 8. Recusar a autorização de visitante

Envie:

```json
{
  "texto": "Quero autorizar a entrada da visitante Maria Oliveira para o dia 25/04/2030."
}
```

A confirmação pendente retornada foi:

```json
{
  "id": "c670bb73-de45-45fb-9c3c-b1c9cef5a081",
  "acao": "autorizar_visitante",
  "detalhes": {
    "nome": "Maria Oliveira",
    "data": "2030-04-25"
  }
}
```

Recuse:

```json
{
  "id": "c670bb73-de45-45fb-9c3c-b1c9cef5a081",
  "confirmado": false
}
```

**Resultado observado:** a API informou que a autorização foi rejeitada. A consulta de visitantes do apartamento 101 retornou somente João Silva; Maria Oliveira não foi cadastrada.

### 9. Autorizar um visitante com data explícita

Envie:

```json
{
  "texto": "Quero autorizar a entrada do visitante Pedro Santos para o dia 26/04/2030."
}
```

**Resultado observado:** a confirmação pendente veio com nome `Pedro Santos` e data `2030-04-26`.

Aprove a confirmação usando o `id` retornado pela API e `confirmado: true`. Nesta execução, a resposta confirmou o cadastro. A consulta:

```bash
curl -s http://localhost:8000/apartamentos/101/visitantes
```

mostrou:

```json
[
  {
    "nome": "João Silva",
    "data": "2025-02-16"
  },
  {
    "nome": "Pedro Santos",
    "data": "2030-04-26"
  }
]
```

**Resultado observado:** a confirmação e a persistência funcionaram, e a data explícita foi interpretada corretamente.

### 10. Conferir os visitantes iniciais de outros apartamentos

Execute:

```bash
curl -s http://localhost:8000/apartamentos/201/visitantes
curl -s http://localhost:8000/apartamentos/302/visitantes
```

**Resultado observado:** o apartamento 201 retornou Paulo Nogueira em `2030-03-23`, e o apartamento 302 retornou Marina Duarte em `2030-03-16`.

### Resumo dos resultados manuais

| Cenário | Resultado |
|---|---|
| Consulta do regulamento da quadra | Aprovado |
| Reserva gratuita da quadra | Aprovado; persistida como `RSV-4822` |
| Reserva paga com aprovação | Aprovado; persistida como `RSV-4823` |
| Recusa de reserva paga | Aprovado; reserva não criada |
| Autorização de João Silva com confirmação | Confirmação e persistência aprovadas; data relativa incorreta |
| Recusa de Maria Oliveira | Aprovado; visitante não cadastrado |
| Autorização de Pedro Santos com data explícita | Aprovado; data persistida corretamente |
| Consulta de visitantes iniciais dos apartamentos 201 e 302 | Aprovado |

## Verificações adicionais

Após os testes manuais descritos acima, foram executadas verificações direcionadas para cancelamento, concorrência, confirmação, persistência e isolamento. Os resultados abaixo são os observados nesta execução; não representam uma nova execução integral de toda a suíte.

### Testes automatizados direcionados

| Comando | Resultado observado | O que cobre |
|---|---|---|
| `uv run pytest -v tests/concorrencia/test_concorrencia_http.py` | `2 passed` em 2,42 s | Duas aprovações simultâneas do mesmo ID: uma resposta HTTP 200 e outra HTTP 409; IDs e sessões diferentes concorrendo pela mesma área/data: apenas uma reserva efetivada. |
| `uv run pytest -v tests/unit/persistencia/test_persistencia.py tests/integracao/test_topologia.py` | `43 passed, 1 skipped` em 1,17 s | Cancelamento, isolamento por apartamento nas ferramentas, confirmação, persistência e topologia dos agentes. O teste ignorado registra uma decisão de produto ainda pendente sobre reservar novamente umadata liberada após cancelamento. |
| `uv run pytest -v tests/integracao/test_regulamento.py tests/integracao/test_topologia.py` | `28 passed` em 1,12 s | Carregamento e consulta do regulamento, limite de artigos retornados, eventos da consulta, ausência de gravação no banco de negócio e ausência do texto completo do regulamento nas instruções do agente principal. |

Os testes direcionados produziram avisos do ADK sobre funcionalidades experimentais (`JSON_SCHEMA_FOR_FUNC_DECL` e `TOOL_CONFIRMATION`). Os avisos não impediram os testes de passar.

### Verificações manuais adicionais pela API

Os cenários seguintes foram executados com a API real em `http://localhost:8000`, usando o Swagger e consultas `curl`.

| Cenário | Resultado observado |
|---|---|
| Reutilizar uma confirmação já aprovada | HTTP 409; a API informou que não havia confirmação pendente com aquele ID na sessão. |
| Enviar um ID de confirmação inexistente | HTTP 409. |
| Enviar mensagem para uma sessão inexistente | HTTP 404, com a mensagem `sessão inexistente`. |
| Tentar obter visitantes dos apartamentos 201 e 302 a partir de uma conversa do apartamento 101 | O agente recusou o pedido e não revelou os dados solicitados. |
| Tentar cadastrar visitante no apartamento 201 a partir da sessão do apartamento 101 | O agente recusou; a consulta posterior do apartamento 201 continuou mostrando apenas o visitante inicial Paulo Nogueira em `2030-03-23`. |
| Reiniciar a API sem executar o comando de restauração | As três reservas então ativas do apartamento 101 e os dois visitantes cadastrados permaneceram disponíveis. A sessão anterior também continuou acessível pelo endpoint de eventos, que respondeu HTTP 200. |
| Cancelar a reserva `RSV-4822` pela conversa da sessão do apartamento 101 | O agente informou sucesso e `GET /apartamentos/101/reservas` confirmou que `RSV-4822` deixou de aparecer, enquanto `RSV-1377` e `RSV-4823` permaneceram ativas. |

O teste manual de cancelamento usou a mensagem `Quero cancelar minha reserva RSV-4822.`. Após a operação, a consulta `curl -s http://localhost:8000/apartamentos/101/reservas` retornou:

```json
[
  {"codigo": "RSV-1377", "area": "quadra", "data": "2030-03-09"},
  {"codigo": "RSV-4823", "area": "salao-de-festas", "data": "2030-04-21"}
]
```

**Limite da evidência de isolamento:** as tentativas manuais de acesso e alteração entre apartamentos foram recusadas nos cenários testados; os testes automatizados também verificam que as ferramentas derivam o apartamento da sessão. Isso não deve ser interpretado como uma auditoria de segurança exaustiva.

## Observação sobre datas relativas

Na solicitação de autorização de João Silva com a expressão “amanhã”, a API retornou `2025-02-16`, enquanto a data explícita `26/04/2030` foi interpretada corretamente como `2030-04-26`. O comportamento pode estar relacionado ao contexto de data usado pelo modelo Gemini ou à forma como a aplicação fornece essa informação; ainda não foi investigado. Por isso, expressões relativas como “amanhã” não foram consideradas validadas neste teste.

## Segurança

- Mantenha `.env` fora do Git.
- Nunca inclua chaves reais em arquivos versionados, logs ou documentação.
- Use sua própria chave do Google AI Studio para executar a API com Gemini.
