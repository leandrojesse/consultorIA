import os
import sys
from dotenv import load_dotenv

# Prefer the new Google GenAI SDK (google-genai). Fall back to the legacy
# google-generativeai package when needed.
try:
    # New SDK: pip install google-genai
    from google import genai
    from google.genai import types as genai_types
    from google.genai import errors as genai_errors
    NEW_GENAI = True
except Exception:
    try:
        # Legacy (deprecated): pip install google-generativeai
        import google.generativeai as genai
        genai_types = None
        genai_errors = None
        NEW_GENAI = False
    except ImportError as e:
        raise ImportError(
            "Instale 'google-genai' (recomendado) ou 'google-generativeai' (legado). Ex.: pip install google-genai"
        ) from e

load_dotenv()  # opcional: carrega .env em desenvolvimento

import logging
from contextlib import contextmanager

# Logger básico para observabilidade local
logger = logging.getLogger("getDados")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
logger.setLevel(os.getenv("GETDADOS_LOG_LEVEL", "INFO"))

# OpenTelemetry tracer (opcional). Se não disponível, fornecemos um span no-op
try:
    from opentelemetry import trace as _trace
    tracer = _trace.get_tracer(__name__)
    OTEL_AVAILABLE = True
except Exception:
    OTEL_AVAILABLE = False
    tracer = None

@contextmanager
def _noop_span(name, attributes=None):
    class DummySpan:
        def set_attribute(self, *_a, **_k):
            pass
        def record_exception(self, *_a, **_k):
            pass
    span = DummySpan()
    yield span

# Helper to start a span: returns a context manager
def start_span(name, attributes=None):
    # Return a context manager-compatible object. Do NOT call __enter__ here; let the
    # caller use `with` to manage context to avoid interfering with opentelemetry's
    # internal context manager implementation (e.g., _AgnosticContextManager).
    if OTEL_AVAILABLE and tracer:
        return tracer.start_as_current_span(name)
    return _noop_span(name, attributes)


# Traceloop (opcional): integra com Dynatrace via OpenTelemetry.
# Inicializamos o SDK apenas se as variáveis necessárias estiverem definidas.
try:
    # Tentar importar o módulo primeiro
    from traceloop.sdk import Traceloop
except Exception:
    TRACELOOP_AVAILABLE = False
    # no-op decorator quando traceloop não está disponível
    def workflow(name=None):
        def _decorator(fn):
            return fn
        return _decorator
else:
    # Só inicializar se houver credenciais/endpoint configurado para evitar warnings do SDK
    tl_api_key = os.getenv("TRACELOOP_API_KEY")
    tl_base = os.getenv("TRACELOOP_BASE_URL")
    tl_headers = os.getenv("TRACELOOP_HEADERS")
    _tl_app = os.getenv("TRACELOOP_APP_NAME", "getDados_app")

    if not (tl_api_key or (tl_base and tl_headers)):
        TRACELOOP_AVAILABLE = False
        def workflow(name=None):
            def _decorator(fn):
                return fn
            return _decorator
    else:
        try:
            # Configure via env vars (Traceloop SDK will read TRACELOOP_BASE_URL/HEADERS or API key)
            Traceloop.init(app_name="tl_app", 
                           disable_batch=True,
                           api_endpoint="https://bvl51829.live.dynatrace.com/api/v2/otlp")
            # Import decorator only after successful init to avoid SDK warnings
            from traceloop.sdk.decorators import workflow
            TRACELOOP_AVAILABLE = True
        except Exception as e:
            TRACELOOP_AVAILABLE = False
            def workflow(name=None):
                def _decorator(fn):
                    return fn
                return _decorator
            print(f"Warning: Traceloop não foi inicializado: {e}")

@workflow(name="perguntar")
def perguntar(pergunta: str) -> str:
    """Faz uma pergunta ao modelo Gemini.

    É necessário definir uma das variáveis de ambiente:
    - GOOGLE_API_KEY (chave de API)
    - GOOGLE_APPLICATION_CREDENTIALS (caminho para JSON da conta de serviço)
    """
    api_key = os.getenv("GOOGLE_API_KEY")
    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

    if not api_key and not cred_path:
        raise RuntimeError(
            "Defina GOOGLE_API_KEY ou GOOGLE_APPLICATION_CREDENTIALS antes de executar."
        )

    # Ajuste o nome do modelo conforme sua conta (p.ex.: 'gemini-pro-latest' ou 'gemini-2.5-flash').
    # Pode ser sobrescrito pela variável de ambiente GENAI_MODEL se preferir outro modelo.
    model_name = os.getenv("GENAI_MODEL", "gemini-pro-latest")

    # Allow overriding max output tokens via env var; default to 65536 to avoid truncation
    try:
        max_output_tokens = int(os.getenv("GENAI_MAX_OUTPUT_TOKENS", "65536"))
    except Exception:
        max_output_tokens = 65536

    # Prefer the new google-genai SDK when available
    if NEW_GENAI:
        client = genai.Client(api_key=api_key) if api_key else genai.Client()
        try:
            # Use typed config when available
            if genai_types:
                base_config = genai_types.GenerateContentConfig(
                    temperature=0.6,
                    max_output_tokens=max_output_tokens,
                )
            else:
                base_config = {"temperature": 0.6, "max_output_tokens": max_output_tokens} 

            # Prepare fallback model list (can be overridden with GENAI_FALLBACK_MODELS env var, comma-separated)
            env_fallback = os.getenv("GENAI_FALLBACK_MODELS")
            if env_fallback:
                fallback_models = [m.strip() for m in env_fallback.split(",") if m.strip()]
            else:
                # Fallback ordered by likely accuracy/quality
                fallback_models = ["gemini-pro-latest", "gemini-3-pro-preview", "gemini-2.5-pro", "gemini-2.5-flash", "gemini-3-flash-preview"]

            models_to_try = [model_name] + [m for m in fallback_models if m != model_name]

            last_not_found_exc = None
            for m in models_to_try:
                try:
                    logger.info("Tentando modelo: %s (prompt_len=%d, max_output_tokens=%d)", m, len(pergunta), max_output_tokens)
                    with start_span("genai.generate_content", attributes={"model": m, "prompt_length": len(pergunta), "max_output_tokens": max_output_tokens}):
                        # Optionally include instructions from the environment (GENAI_INSTRUCTIONS).
                        # If not set, send the user prompt as-is (avoids using role-based message structures
                        # that some SDK versions don't accept).
                        instructions = os.getenv("GENAI_INSTRUCTIONS")
                        if instructions:
                            instruction_snippet = instructions[:256]
                            prompt_for_model = f"INSTRUCTIONS:\n{instructions}\n\nUSER_QUESTION:\n{pergunta}"
                        else:
                            instruction_snippet = None
                            prompt_for_model = pergunta

                        resp = client.models.generate_content(
                            model=m,
                            contents=prompt_for_model,
                            config=base_config,
                        )
                        # Attach the instruction snippet to the span for observability
                        try:
                            if instruction_snippet and OTEL_AVAILABLE:
                                _trace.get_current_span().set_attribute("instruction_snippet", instruction_snippet)
                        except Exception:
                            pass
                        # Extract text from various SDK response shapes
                        response_text = None
                        if hasattr(resp, "output_text") and resp.output_text:
                            response_text = resp.output_text
                        elif hasattr(resp, "text") and resp.text:
                            response_text = resp.text
                        elif hasattr(resp, "candidates") and resp.candidates:
                            try:
                                response_text = resp.candidates[0].content
                            except Exception:
                                response_text = None
                        else:
                            # Fallback: try to collect text fragments from resp.output
                            texts = []
                            for item in getattr(resp, "output", []):
                                content = None
                                if isinstance(item, dict):
                                    content = item.get("content", [])
                                else:
                                    content = getattr(item, "content", [])
                                for c in content:
                                    if isinstance(c, dict):
                                        if c.get("type") == "output_text":
                                            texts.append(c.get("text", ""))
                                        elif c.get("text"):
                                            texts.append(c.get("text"))
                                    else:
                                        if getattr(c, "type", None) == "output_text":
                                            texts.append(getattr(c, "text", ""))
                                        elif getattr(c, "text", None):
                                            texts.append(getattr(c, "text"))
                            if texts:
                                response_text = "\n".join(texts)

                        if response_text is None:
                            response_text = str(resp)

                        # Set span attributes and logs
                        try:
                            if OTEL_AVAILABLE:
                                _trace.get_current_span().set_attribute("model_used", m)
                                _trace.get_current_span().set_attribute("response_length", len(response_text))
                                _trace.get_current_span().set_attribute("prompt_length", len(pergunta))
                        except Exception:
                            pass

                        logger.info("Usando modelo: %s; response_length=%d", m, len(response_text))

                        # Warn if response approaches the max tokens (possible truncation)
                        try:
                            if len(response_text) >= int(max_output_tokens * 0.9):
                                logger.warning("Resposta com %d chars aproxima max_output_tokens=%d (possível truncamento)", len(response_text), max_output_tokens)
                                try:
                                    if OTEL_AVAILABLE:
                                        _trace.get_current_span().set_attribute("possible_truncation", True)
                                except Exception:
                                    pass
                        except Exception:
                            pass

                        return response_text
                except Exception as e2:
                    # If model is not found (404) or quota exhausted (429), try next model in fallback list
                    should_continue = False
                    try:
                        if genai_errors and isinstance(e2, genai_errors.ClientError):
                            code = getattr(e2, "code", None)
                            if code == 404 or "NOT_FOUND" in str(e2):
                                should_continue = True
                            if code == 429 or "RESOURCE_EXHAUSTED" in str(e2) or "quota" in str(e2).lower():
                                should_continue = True
                    except Exception:
                        pass

                    if should_continue:
                        last_not_found_exc = e2
                        continue
                    # For other errors, re-raise
                    raise

            # If we get here, none of the models worked; list models to provide guidance
            try:
                models = client.models.list(config={"page_size": 200})
                model_names = [m.name for m in models]
                suggestions = [n.split("/", 1)[-1] for n in model_names if "gemini" in n][:6]
                raise RuntimeError(
                    f"Tentativas falharam para modelos {', '.join(models_to_try)}. Sugestões: {', '.join(suggestions)}\nModelos completos (exemplos): {', '.join(model_names[:20])}"
                ) from last_not_found_exc
            except Exception:
                raise RuntimeError(
                    f"Tentativas falharam para modelos {', '.join(models_to_try)}. (Falha ao listar modelos; verifique sua chave e permissões.)"
                ) from last_not_found_exc
        finally:
            try:
                client.close()
            except Exception:
                pass

    # Legacy compatibility: older google-generativeai package
    # versões antigas tinham genai.generate_text; as novas usam Responses API
    # Legacy compatibility: older google-generativeai package
    # Tentativa com fallback de modelos semelhante ao cliente novo
    env_fallback = os.getenv("GENAI_FALLBACK_MODELS")
    if env_fallback:
        fallback_models = [m.strip() for m in env_fallback.split(",") if m.strip()]
    else:
        # Legacy fallback order: prioritize higher-quality models first
        fallback_models = ["gemini-pro-latest", "gemini-3-pro-preview", "gemini-2.5-pro", "gemini-2.5-flash", "gemini-3-flash-preview"]

    models_to_try = [model_name] + [m for m in fallback_models if m != model_name]

    if hasattr(genai, "generate_text"):
        for m in models_to_try:
            try:
                logger.info("Tentando modelo (legacy): %s (prompt_len=%d, max_output_tokens=%d)", m, len(pergunta), max_output_tokens)
                with start_span("genai.generate_text", attributes={"model": m, "prompt_length": len(pergunta), "max_output_tokens": max_output_tokens}):
                    resp = genai.generate_text(
                        model=m,
                        prompt=pergunta,
                        max_output_tokens=max_output_tokens,
                        temperature=0.6,
                    )
                    response_text = None
                    try:
                        response_text = resp.candidates[0].content
                    except Exception:
                        response_text = str(resp)

                    try:
                        if OTEL_AVAILABLE:
                            _trace.get_current_span().set_attribute("model_used", m)
                            _trace.get_current_span().set_attribute("response_length", len(response_text))
                            _trace.get_current_span().set_attribute("prompt_length", len(pergunta))
                    except Exception:
                        pass

                    logger.info("Usando modelo (legacy): %s; response_length=%d", m, len(response_text))

                    try:
                        if len(response_text) >= int(max_output_tokens * 0.9):
                            logger.warning("Resposta com %d chars aproxima max_output_tokens=%d (possível truncamento)", len(response_text), max_output_tokens)
                            try:
                                if OTEL_AVAILABLE:
                                    _trace.get_current_span().set_attribute("possible_truncation", True)
                            except Exception:
                                pass
                    except Exception:
                        pass

                    return response_text
            except Exception as e:
                if "NOT_FOUND" in str(e) or "not found" in str(e).lower() or "RESOURCE_EXHAUSTED" in str(e) or "quota" in str(e).lower():
                    continue
                raise

    if hasattr(genai, "responses") and hasattr(genai.responses, "create"):
        for m in models_to_try:
            resp = None
            try:
                logger.info("Tentando modelo (legacy:responses): %s (prompt_len=%d, max_output_tokens=%d)", m, len(pergunta), max_output_tokens)
                with start_span("genai.responses.create", attributes={"model": m, "prompt_length": len(pergunta), "max_output_tokens": max_output_tokens}):
                    resp = genai.responses.create(
                        model=m,
                        input=pergunta,
                        max_output_tokens=max_output_tokens,
                        temperature=0.6,
                    )

                    response_text = None
                    # Prefer convenience property if disponível
                    if hasattr(resp, "output_text") and resp.output_text:
                        response_text = resp.output_text
                    else:
                        # Tentar extrair texto de resp.output
                        texts = []
                        for item in getattr(resp, "output", []):
                            content = None
                            if isinstance(item, dict):
                                content = item.get("content", [])
                            else:
                                content = getattr(item, "content", [])
                            for c in content:
                                if isinstance(c, dict):
                                    if c.get("type") == "output_text":
                                        texts.append(c.get("text", ""))
                                    elif c.get("text"):
                                        texts.append(c.get("text"))
                                else:
                                    if getattr(c, "type", None) == "output_text":
                                        texts.append(getattr(c, "text", ""))
                                    elif getattr(c, "text", None):
                                        texts.append(getattr(c, "text"))
                        if texts:
                            response_text = "\n".join(texts)

                    if response_text is None:
                        response_text = str(resp)

                    try:
                        if OTEL_AVAILABLE:
                            _trace.get_current_span().set_attribute("model_used", m)
                            _trace.get_current_span().set_attribute("response_length", len(response_text))
                            _trace.get_current_span().set_attribute("prompt_length", len(pergunta))
                    except Exception:
                        pass

                    logger.info("Usando modelo (legacy:responses): %s; response_length=%d", m, len(response_text))

                    try:
                        if len(response_text) >= int(max_output_tokens * 0.9):
                            logger.warning("Resposta com %d chars aproxima max_output_tokens=%d (possível truncamento)", len(response_text), max_output_tokens)
                            try:
                                if OTEL_AVAILABLE:
                                    _trace.get_current_span().set_attribute("possible_truncation", True)
                            except Exception:
                                pass
                    except Exception:
                        pass

                    return response_text
            except Exception as e:
                if "NOT_FOUND" in str(e) or "not found" in str(e).lower():
                    continue
                # Outros erros: re-raise
                raise

    # If we reach here, the installed package is not compatible
    raise AttributeError(
        "Nenhuma API compatível encontrada. Instale 'google-genai' (recomendado): pip install --upgrade google-genai"
    )


def listar_modelos():
    """Lista modelos disponíveis da conta (tenta SDK novo primeiro)."""
    if NEW_GENAI:
        client = genai.Client()
        try:
            for m in client.models.list(config={"page_size": 500}):
                print(m.name)
        finally:
            try:
                client.close()
            except Exception:
                pass
    else:
        # SDK legado pode não expor listagem centralizada; tentamos heurística
        try:
            if hasattr(genai, "models") and hasattr(genai.models, "list"):
                for m in genai.models.list():
                    print(m.name)
                return
        except Exception:
            pass

    print("Could not list models: SDK does not support listing models from this environment.")


if __name__ == "__main__":
    # Suporta flags: --list-models
    if "--list-models" in sys.argv or "-l" in sys.argv:
        listar_modelos()
        sys.exit(0)

    # Suporta uso interativo ou via argumentos de linha de comando:
    # Ex.: python getDados.py "Qual é a capital do Brasil?"
    if len(sys.argv) > 1:
        pergunta = " ".join(arg for arg in sys.argv[1:] if arg not in ("--list-models", "-l")).strip()
    else:
        try:
            pergunta = input("Digite sua pergunta para o modelo: ").strip()
        except EOFError:
            print("Nenhuma pergunta fornecida.", file=sys.stderr)
            sys.exit(1)

    if not pergunta:
        print("Pergunta vazia. Abortando.", file=sys.stderr)
        sys.exit(1)

    resposta = perguntar(pergunta)
    print(resposta)
    # Exit to avoid executing any leftover example lines below
    sys.exit(0)
    pergunta = "Qual é a capital do Brasil?"
    print(perguntar(pergunta))