# Desafio: agente da Doris

## Contexto

A Doris é uma assistente de moda. A pessoa conversa com ela no app, e a Doris busca peças, recomenda
looks, gera um **try-on** (a peça vestida no avatar da pessoa) e opina sobre o que ela está usando.

Neste repositório você já recebe tudo **menos o agente**:

- **Frontend** de chat (em `http://localhost:3000`).
- **Proxy**: o serviço que fica entre o app e o agente. Ele repassa cada mensagem ao agente (com o perfil básico da
  pessoa), devolve a resposta em streaming para o frontend e expõe as **tools** (busca, criação de peça, try-on,
  guarda-roupa, looks…).
- **Dados de teste**: 3 usuários (Ana, Bruno e Carla), cada um com avatar, guarda-roupa e looks anteriores, e um
  catálogo de produtos fictícios.

```
frontend ──▶ proxy /chat ──▶ seu agente (agent/)
                ▲                  │
                └──── /tools/* ◀───┘          seu agente ──▶ OpenRouter
```

## Sua tarefa

Construir o agente em `agent/`.

- **O escopo é maior que 4 horas, de propósito.** Não esperamos tudo pronto: queremos ver o que você escolhe fazer
  primeiro e com que qualidade. Priorizar faz parte da avaliação.
- **As 4 horas começam no `git clone`.** Não há cronômetro; confiamos em você. Anote início e fim no seu README.
- **Pode (e deve) usar IA**, com bom senso. Você precisa saber explicar tudo o que entregar.
- Recomendamos **LangGraph**, mas não é obrigatório. Qualquer linguagem serve, desde que rode no Docker e siga o
  contrato abaixo.

## O que esperamos do agente

O fluxo principal: a pessoa pede algo → o agente **busca** → **escolhe** bons itens → **cria a peça**
(`garment_creation`) → gera o **try-on** → **mostra** o resultado para a pessoa.

Cenários:

| # | Usuário | Mensagem | O que observamos |
|---|---|---|---|
| 1 | Ana | "Me acha uma camisa de linho branca até R$300" | Busca, respeito ao preço, apresentação dos produtos |
| 2 | Bruno | "Tenho um casamento na praia sábado, o que eu visto?" | Fluxo completo até o try-on |
| 3 | Carla | "O que você acha do meu último look?" | Opinião honesta sobre a imagem do look |
| 4 | Ana | Conversa A: "Odeio amarelo, visto M e curto estilo minimalista." Conversa **nova** B: "Me sugere algo pra um jantar" | Memória entre conversas |
| 5 | Ana | "Tem jaqueta jeans?" e depois "Quero provar a jaqueta de couro preta" | Falhas: busca vazia e try-on que falha uma vez |
| 6 | Bruno | "Oi" e "Qual a capital da França?" | Conversa fora do tema |

Qualidades que valorizamos: o agente é **robusto** (não entra em loop, lida bem com falhas e lentidão),
**lembra da pessoa entre conversas**, **classifica as tarefas e usa modelos diferentes para tarefas diferentes**,
**cuida do custo**, é **observável** e **dá feedback** do que está fazendo enquanto as tools trabalham.

Os cenários que usamos na avaliação são **variações** destes, com outras palavras, outros usuários e outras falhas.

## Como rodar

```bash
cp .env.example .env              # coloque a chave da OpenRouter em OPENROUTER_API_KEY
docker compose up --build         # proxy + agente
# abra http://localhost:3000
```

- Para depurar: `GET http://localhost:3000/admin/log?conversation_id=<id>` mostra os turnos e as chamadas de tools
  registradas pelo proxy.
- Os testes do proxy rodam com `pip install -r requirements-dev.txt && pytest`.

## Contrato do agente

O proxy chama o seu agente em `AGENT_URL` (padrão `http://agent:8000`). O agente chama as tools em `PROXY_URL`
(padrão `http://proxy:3000`).

### `POST /chat`

```json
{
  "user_id": "u_ana",
  "conversation_id": "5f0c…",
  "message": "Tenho um casamento na praia sábado, o que eu visto?",
  "images": [],
  "profile": { "name": "Bruno", "gender": "MALE", "city": "Rio de Janeiro" }
}
```

- `profile`: perfil básico da pessoa, injetado pelo proxy em toda chamada (nome, gênero, cidade). Não traz
  gostos nem preferências.
- `images`: imagens anexadas pela pessoa nesta mensagem (URL pública ou `data:image/...;base64,...`).
- Cada chamada traz **só a mensagem atual**. O proxy não envia o histórico: guardar a conversa e decidir o que vai
  para cada chamada de modelo é responsabilidade do agente.
- Um `user_id` pode aparecer com sufixo (`u_ana__abc`). Trate cada `user_id` como uma pessoa diferente.

A resposta é `text/event-stream`. Cada evento é uma linha `data: <json>` seguida de uma linha em branco:

| Evento | Para que serve |
|---|---|
| `{"type": "text", "delta": "…"}` | Pedaço do texto da resposta (zero ou mais) |
| `{"type": "feedback", "id": "tryon-1", "text": "Gerando seu try-on…", "state": "running"}` | Linha de progresso. O frontend mostra uma linha por `id`; um evento novo com o mesmo `id` atualiza a linha. `state`: `running` (spinner), `done` (✓) ou `error` (✗) |
| `{"type": "done"}` | Fim do turno (exatamente um, por último) |
| `{"type": "error", "message": "…"}` | Fim do turno com erro (no lugar do `done`) |

Para **mostrar** produtos, peças e try-ons para a pessoa, o agente chama `POST /tools/show_artifact`. O proxy
coloca o card no chat. Um try-on que nunca é mostrado não aparece para a pessoa.

O turno é **encerrado pelo proxy em 100 segundos**.

### `GET /health`

Responde 200 quando o agente está pronto.

As tools estão documentadas em [`docs/proxy-api.md`](docs/proxy-api.md).

## Modelos disponíveis

A chave que enviamos dá acesso a estes modelos na [OpenRouter](https://openrouter.ai):

- [`openai/gpt-6-luna`](https://openrouter.ai/openai/gpt-6-luna)
- [`google/gemini-3.8-flash`](https://openrouter.ai/google/gemini-3.8-flash)
- [`google/gemma-4-26b-a4b-it`](https://openrouter.ai/google/gemma-4-26b-a4b-it)
- [`qwen/qwen3.5-9b`](https://openrouter.ai/qwen/qwen3.5-9b)
- [`~typesafe/jev-latest`](https://openrouter.ai/~typesafe/jev-latest)
- [`openai/text-embedding-3-small`](https://openrouter.ai/openai/text-embedding-3-small)

## Regras

- Altere apenas `agent/` e o `docker-compose.yml`: você pode mudar o serviço `agent` (volumes, variáveis,
  `depends_on`) e adicionar serviços (banco, cache, tracing…).
- Não altere `proxy/`, `seed-images/`, os dados de teste ou `proxy/faults.json`: na avaliação usamos a
  nossa cópia deles.
- O agente não pode chamar `/admin/*` nem `/conversations/*`. Esses endpoints existem só para depuração, para a nossa
  avaliação e para o histórico do frontend.
- A única chamada externa permitida é para a OpenRouter, com a chave fornecida. Todo o resto roda dentro do compose.
- Nunca faça commit da chave.

## Chave da OpenRouter

Sua chave está aqui: **<LINK_ONETIMESECRET>**

O link funciona **uma única vez**. Se ele disser que já foi visualizado, nos avise antes de continuar. A chave tem
um limite de gasto e é revogada depois do prazo.

## Backlog

Mais do que cabe em 4 horas, sem ordem de prioridade:

- Fluxo completo de busca → escolha → peça → try-on → mostrar.
- Todos os cenários acima.
- Memória de longo prazo da pessoa entre conversas.
- Histórico da conversa e engenharia de contexto.
- Classificação de intenção e roteamento entre modelos.
- Tratamento de falhas, timeouts e limites de execução.
- Feedback de progresso durante as tools.
- Controle e medição de custo.
- Observabilidade (traces, métricas, logs).
- Proteção contra prompt injection.
- Refinamento em vários turnos ("não gostei, tem em azul?").
- Uso de imagens enviadas pela pessoa.
- Testes automatizados e uma avaliação própria.

## O que avaliamos

Robustez · cuidado com custo · memória · observabilidade · prompts e engenharia de contexto · roteamento entre
modelos · qualidade nos cenários e feedback das tools · engenharia de software e o seu README.

## Entrega

1. Clique em **Use this template** e crie um repositório **público** na sua conta.
2. Trabalhe na branch **`solucao`**.
3. Abra um **pull request** de `solucao` para o `main` do seu repositório.
4. Envie o link do PR para nós.

O arquivo `agent/README.md` é seu. Ele precisa ter: ideia e arquitetura; decisões e trade-offs; o que ficou de fora
e por quê; próximos passos; como você usou IA (onde ajudou e onde errou); horário de início e de fim; como rodar.
