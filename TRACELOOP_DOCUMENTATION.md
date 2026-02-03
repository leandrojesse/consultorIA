# Documentação: Integração do Traceloop em `getDados.py` ✅

**Resumo rápido:** o módulo integra o Traceloop (Dynatrace) de forma *opcional* e segura — tenta inicializar o SDK apenas se as variáveis de ambiente necessárias estiverem configuradas; caso contrário, fornece um *decorator* `workflow` no-op para não quebrar a execução. A função `perguntar` já está decorada com `@workflow(name="perguntar")`, portanto reporta como *workflow* quando o Traceloop está ativo. 💡

---

## 1) Onde fica no código 🔎
- Arquivo: `getDados.py`
- Trechos relevantes:
  - Import condicional: `from traceloop.sdk import Traceloop`
  - Inicialização condicional: `Traceloop.init(...)`
  - Import do decorator: `from traceloop.sdk.decorators import workflow`
  - Decorator aplicado: `@workflow(name="perguntar")` sobre a função `perguntar`

---

## 2) Comportamento esperado / fluxo 📈
- O módulo tenta importar o SDK do Traceloop.
  - Se a importação falhar → `TRACELOOP_AVAILABLE = False` e um `workflow` no-op é definido (retorna a função original).
  - Se importar com sucesso → verifica presença de credenciais/endpoint via variáveis de ambiente; só inicializa o SDK se configurado corretamente.
- Ao inicializar com sucesso, importa-se o decorator `workflow` e passa-se a reportar workflows/executions para o backend.

---

## 3) Variáveis de ambiente relevantes 🧩
- Opcionalmente necessárias para habilitar o Traceloop:
  - `TRACELOOP_API_KEY` — chave de API (forma direta de habilitar)
  - `TRACELOOP_BASE_URL` e `TRACELOOP_HEADERS` — alternativa para configurar endpoint/headers
  - `TRACELOOP_APP_NAME` — nome da aplicação (o código tem leitura desta variável)

**Exemplo `.env`:**

```env
TRACELOOP_API_KEY=xxxxxx
TRACELOOP_APP_NAME=getDados_app
# ou
TRACELOOP_BASE_URL=https://<seu-endpoint>
TRACELOOP_HEADERS=<json-ou-string-de-headers>
```

---

## 4) Integração com OpenTelemetry 🔧
- O módulo tenta importar `opentelemetry` e, se presente, cria `tracer = _trace.get_tracer(__name__)`.
- Existe o helper `start_span(name, attributes=None)` que retorna um span real quando OTEL está disponível, ou um `_noop_span` quando não.
- Dentro da função `perguntar` são criados spans (`genai.generate_content`, `genai.generate_text`, etc.) e, se OTEL estiver disponível, atributos úteis são definidos: `model_used`, `response_length`, `prompt_length`, `possible_truncation`, e `instruction_snippet`.

---

## 5) Comportamento do decorator `@workflow` ✅
- Quando Traceloop ativo: o decorator reporta o workflow/execution ao serviço Dynatrace via SDK.
- Quando Traceloop não ativo: o decorator é no-op — **não afeta** a execução do código.

> **Observação importante:** o código lê `TRACELOOP_APP_NAME` em `_tl_app = os.getenv("TRACELOOP_APP_NAME", "getDados_app")`, porém na chamada `Traceloop.init(...)` o `app_name` passado é o literal `"tl_app"`. Isso aparenta ser uma inconsistência (provável bug) — recomenda-se usar `_tl_app` na chamada de inicialização.

---

## 6) Verificação e troubleshooting ⚠️
- Mensagens de aviso: quando a inicialização falha o código imprime `Warning: Traceloop não foi inicializado: {e}`.
- Testes manuais sugeridos:
  1. Habilitar as variáveis de ambiente (ex.: `TRACELOOP_API_KEY`).
  2. Executar: `python getDados.py "Qual é a capital do Brasil?"`.
  3. Verificar no Dynatrace se o workflow/execution foi registrado.
- Sem variáveis: o módulo continua funcionando sem enviar telemetry/traces.
- Erros comuns: SDK ausente, endpoint ou headers inválidos, ou chave/API malformada.

---

## 7) Sugestões de melhoria 💡
- Corrigir o bug do `app_name` — usar a variável `_tl_app` ao chamar `Traceloop.init(...)`.
- Permitir configurar `api_endpoint` via variável de ambiente (ex.: `TRACELOOP_API_ENDPOINT`) em vez de codificar o endpoint no código.
- Adicionar logs de sucesso explícitos (ex.: `logger.info("Traceloop inicializado com sucesso: %s", _tl_app)`).
- Enriquecer spans/workflows com identificadores e captura de exceções (mesmo em fallback) para melhor observabilidade.
- Adicionar testes automatizados cobrindo os casos: SDK ausente, credenciais ausentes, credenciais válidas e inicialização bem-sucedida.

---

## 8) Como ativar/usar 🛠️
1. Instale o SDK (se necessário): `pip install traceloop` (ver documentação do fornecedor).
2. Configure as variáveis de ambiente (API key ou base+headers).
3. Execute o script normalmente. Quando ativo, o `@workflow(name="perguntar")` irá enviar execução para Dynatrace.

---

## 9) Próximos passos / opções 🔭
- Posso criar um patch para:
  - corrigir `app_name` para usar `TRACELOOP_APP_NAME`,
  - permitir `TRACELOOP_API_ENDPOINT` via env var, e
  - adicionar logs de inicialização/sucesso.

---

Se desejar, gero também um _pull request_ (patch) com as correções e testes sugeridos. 🔧