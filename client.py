#!/usr/bin/env python3
"""
Cliente MCP para perguntas em linguagem natural sobre o SIGARRA.
"""

import base64
import asyncio
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import webbrowser

if os.name == 'nt':
    import msvcrt
else:
    from getpass import getpass

import httpx
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

def get_password(prompt="Password: "):
    if os.name == 'nt':
        print(prompt, end="", flush=True)
        password = ""
        while True:
            char = msvcrt.getch()
            if char == b'\r' or char == b'\n':
                print()
                break
            elif char == b'\b':
                if password:
                    password = password[:-1]
                    print('\b \b', end="", flush=True)
            else:
                password += char.decode('utf-8', errors='ignore')
                print('*', end="", flush=True)
        return password
    else:
        from getpass import getpass
        return getpass(prompt)

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------
load_dotenv()

API_ENDPOINT = os.getenv("API_ENDPOINT", "https://api.iaedu.pt/agent-chat/api/v1/agent/cmamvd3n40000c801qeacoad2/stream")
API_KEY = os.getenv("API_KEY", "")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")
SERVER_SCRIPT = Path(__file__).parent / "server.py"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


OIDC_DISCOVERY_URL = os.getenv(
    "OIDC_DISCOVERY_URL",
    "https://open-id.up.pt/realms/sigarra/.well-known/openid-configuration",
).strip()
OIDC_CLIENT_ID = os.getenv("OIDC_CLIENT_ID", "").strip()
OIDC_CLIENT_SECRET = os.getenv("OIDC_CLIENT_SECRET", "").strip()
OIDC_REDIRECT_URI = os.getenv("OIDC_REDIRECT_URI", "http://127.0.0.1:8765/callback").strip()
OIDC_SCOPE = os.getenv("OIDC_SCOPE", "openid profile email").strip()
OIDC_IDP_HINT = os.getenv("OIDC_IDP_HINT", "saml").strip()
OIDC_TIMEOUT_SECONDS = _env_int("OIDC_TIMEOUT_SECONDS", 300)

DEFAULT_SOURCE_URL = "https://sigarra.up.pt/feup/pt/web_page.inicial"
SOURCE_URLS = {
    "teacher": "https://sigarra.up.pt/feup/pt/mob_func_geral.pesquisa",
    "course": "https://sigarra.up.pt/feup/pt/ucurr_geral.pesquisa_ocorr_ucs_list",
    "course_schedule_mobile": "https://sigarra.up.pt/feup/pt/mob_hor_geral.ucurr",
    "calendar": "https://sigarra.up.pt/feup/pt/web_base.gera_pagina?p_pagina=calend%c3%a1rio%20escolar",
    "canteen": "https://sigarra.up.pt/feup/pt/mob_eme_geral.cantinas",
    "parking": "https://sigarra.up.pt/feup/pt/instalacs_geral.ocupacao_parques",
    "schedule": "https://sigarra.up.pt/feup/pt/mob_hor_geral.estudante",
    "exams": "https://sigarra.up.pt/feup/pt/mob_fest_geral.exames",
    "profile": "https://sigarra.up.pt/feup/pt/mob_fest_geral.percurso_academico",
    "grades": "https://sigarra.up.pt/feup/pt/mob_fest_geral.percurso_academico",
    "enrollments": "https://sigarra.up.pt/feup/pt/mob_fest_geral.ucurr_inscricoes_corrente",
    "current_account": "https://sigarra.up.pt/feup/pt/gpag_ccorrente_geral.conta_corrente_view",
}
COURSE_INFO_VIEW_URL = "https://sigarra.up.pt/feup/pt/ucurr_geral.ficha_uc_view"
SOURCE_URLS_BY_CONTEXT = {
    "horário": SOURCE_URLS["schedule"],
    "horário da uc": SOURCE_URLS["course_schedule_mobile"],
    "exames": SOURCE_URLS["exams"],
    "perfil": SOURCE_URLS["profile"],
    "notas": SOURCE_URLS["grades"],
    "inscrições": SOURCE_URLS["enrollments"],
    "conta corrente": SOURCE_URLS["current_account"],
    "uc": SOURCE_URLS["course"],
    "parques": SOURCE_URLS["parking"],
    "cantina": SOURCE_URLS["canteen"],
    "calendário": SOURCE_URLS["calendar"],
    "docentes": SOURCE_URLS["teacher"],
}
AUTH_INTENTS = {"schedule", "exams", "profile", "grades", "enrollments", "current_account"}
AUTH_CONTEXT_LABELS = {"horário", "exames", "perfil", "notas", "inscrições", "conta corrente"}

_conversation_context = {"type": None, "data": None}
_last_ask_meta = {"tools_used": [], "mcp_calls": 0}


def get_last_ask_meta() -> dict:
    return {
        "tools_used": list(_last_ask_meta.get("tools_used", [])),
        "mcp_calls": int(_last_ask_meta.get("mcp_calls", 0)),
    }


def _is_follow_up_question(question_lower: str, has_new_intent: bool) -> bool:
    """Deteta follow-up anafórico curto (ex.: "e o p3?") e evita confundir com tema novo."""
    if has_new_intent:
        return False

    cleaned = re.sub(r"\s+", " ", question_lower).strip(" .?!,;:")
    if not cleaned:
        return False

    explicit_patterns = [
        r"\bqual delas\b",
        r"\bqual é\b",
        r"\bquais\b",
        r"\bquantas\b",
        r"\bquantos\b",
        r"\bmostra\b",
        r"\bqual tem\b",
        r"\bmaior\b",
    ]
    if any(re.search(pattern, cleaned) for pattern in explicit_patterns):
        return True

    if not re.match(r"^e\b", cleaned):
        return False

    # Follow-up com "e ..." deve ser curto e com referência ao contexto anterior.
    tokens = cleaned.split()
    if len(tokens) > 5:
        return False

    short_reference_patterns = [
        r"^e\s+(o|a|os|as)\b",
        r"^e\s+(no|na|nos|nas|do|da|dos|das)\b",
        r"^e\s+p\d+\b",
        r"^e\s+qual\b",
        r"^e\s+quais\b",
        r"^e\s+quantos\b",
        r"^e\s+quantas\b",
    ]
    return any(re.search(pattern, cleaned) for pattern in short_reference_patterns)


def _append_query_params(url: str, params: dict[str, str]) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(params)
    new_query = urlencode(query)
    return urlunparse(parsed._replace(query=new_query))


def _week_range_strings(weeks_ahead: int = 16) -> tuple[str, str]:
    today = datetime.now()
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6 + weeks_ahead * 7)
    return week_start.strftime("%Y%m%d"), week_end.strftime("%Y%m%d")


def _detect_response_language(question: str) -> str:
    """Deteta rapidamente se a pergunta foi escrita em ingles ou portugues."""
    q = question.lower().strip()
    if not q:
        return "pt"

    portuguese_markers = {
        "horário", "horario", "calendário", "cantina", "estacionamento", "docente",
        "inscrições", "inscricoes", "cadeira", "disciplina", "feriado", "época",
        "meu", "minha", "quero", "qual", "quando", "onde", "como", "obrigado",
    }
    english_markers = {
        "schedule", "calendar", "canteen", "parking", "teacher", "course",
        "grade", "grades", "enrollment", "enrollments", "what", "when", "where",
        "how", "please", "thanks", "my", "i", "can", "could", "would",
    }

    words = {w for w in re.findall(r"[a-zA-Z]+", q) if w}
    if not words:
        return "pt"

    pt_score = sum(1 for w in words if w in portuguese_markers)
    en_score = sum(1 for w in words if w in english_markers)

    if re.search(r"[áàâãéêíóôõúç]", q):
        pt_score += 2

    return "en" if en_score > pt_score else "pt"


def _oidc_enabled() -> bool:
    return bool(OIDC_CLIENT_ID)


async def _fetch_oidc_metadata() -> dict:
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        response = await client.get(OIDC_DISCOVERY_URL, headers=HEADERS)
        response.raise_for_status()
        metadata = response.json()

    if not isinstance(metadata, dict):
        raise ValueError("Discovery OIDC inválido.")
    return metadata


def _build_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _extract_oidc_username(id_token: str) -> str:
    if not id_token:
        return ""

    try:
        parts = id_token.split(".")
        if len(parts) < 2:
            return ""
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        username = str(claims.get("preferred_username") or claims.get("sub") or "").strip()
    except Exception:
        return ""

    if "@" in username:
        username = username.split("@", 1)[0]
    if username.lower().startswith("up"):
        username = username[2:]
    return username.strip()


def _parse_oidc_callback_response(raw_response: str) -> dict[str, str]:
    raw_response = (raw_response or "").strip()
    if not raw_response:
        return {}

    if raw_response.startswith(("http://", "https://")):
        parsed = urlparse(raw_response)
        data = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if parsed.fragment:
            data.update(dict(parse_qsl(parsed.fragment, keep_blank_values=True)))
        return data

    if "=" in raw_response and "&" in raw_response:
        return dict(parse_qsl(raw_response, keep_blank_values=True))

    return {"code": raw_response}


def _is_local_redirect_uri(redirect_uri: str) -> bool:
    parsed = urlparse(redirect_uri or "")
    return parsed.scheme in {"http", "https"} and parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def _wait_for_oidc_callback(redirect_uri: str, auth_url: str, expected_state: str) -> dict[str, str]:
    parsed_redirect = urlparse(redirect_uri)
    host = parsed_redirect.hostname or "127.0.0.1"
    port = parsed_redirect.port or (443 if parsed_redirect.scheme == "https" else 80)
    expected_path = parsed_redirect.path or "/"
    callback_event = threading.Event()
    callback_data: dict[str, str] = {}

    class OIDCCallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed_request = urlparse(self.path)
            if parsed_request.path != expected_path:
                self.send_error(404, "Not Found")
                return

            params = dict(parse_qsl(parsed_request.query, keep_blank_values=True))
            callback_data.clear()
            callback_data.update(params)

            if params.get("state") and params.get("state") != expected_state:
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    "<html><body><h3>Estado OIDC inválido.</h3>"
                    "<p>Podes fechar esta janela e voltar ao terminal.</p></body></html>".encode("utf-8")
                )
                callback_event.set()
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(
                "<html><body><h3>Autenticação concluída.</h3>"
                "<p>Podes regressar ao terminal.</p></body></html>".encode("utf-8")
            )
            callback_event.set()

        def log_message(self, format, *args):  # noqa: A003
            return

    try:
        server = HTTPServer((host, port), OIDCCallbackHandler)
    except OSError as exc:
        raise RuntimeError(f"Não foi possível abrir o callback local em {host}:{port}: {exc}") from exc

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        print("Abre o navegador para concluir a autenticação federada:")
        print(auth_url)
        try:
            webbrowser.open(auth_url, new=1, autoraise=True)
        except Exception:
            pass

        if not callback_event.wait(OIDC_TIMEOUT_SECONDS):
            raise TimeoutError("Tempo esgotado à espera do retorno OIDC.")
        return callback_data
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


async def _exchange_oidc_code(token_endpoint: str, code: str, code_verifier: str) -> dict:
    data = {
        "grant_type": "authorization_code",
        "client_id": OIDC_CLIENT_ID,
        "code": code,
        "redirect_uri": OIDC_REDIRECT_URI,
        "code_verifier": code_verifier,
    }
    if OIDC_CLIENT_SECRET:
        data["client_secret"] = OIDC_CLIENT_SECRET

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        response = await client.post(
            token_endpoint,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Falha ao trocar o code por token: {response.text.strip() or response.status_code}")
        payload = response.json()

    if not isinstance(payload, dict):
        raise RuntimeError("Resposta OIDC inválida no token endpoint.")
    return payload


async def _resolve_oidc_username(metadata: dict, access_token: str, id_token: str) -> str:
    username = _extract_oidc_username(id_token)
    if username:
        return username

    userinfo_endpoint = str(metadata.get("userinfo_endpoint", "")).strip()
    if not userinfo_endpoint:
        return ""

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.get(
                userinfo_endpoint,
                headers={**HEADERS, "Authorization": f"Bearer {access_token}"},
            )
            if response.status_code >= 400:
                return ""
            payload = response.json()
    except Exception:
        return ""

    if not isinstance(payload, dict):
        return ""

    username = str(payload.get("preferred_username") or payload.get("sub") or "").strip()
    if "@" in username:
        username = username.split("@", 1)[0]
    if username.lower().startswith("up"):
        username = username[2:]
    return username.strip()


async def _login_oidc(session: ClientSession, verbose: bool = True) -> bool:
    if not _oidc_enabled():
        if verbose:
            print("Autenticação federada indisponível: falta OIDC_CLIENT_ID.")
        return False

    try:
        metadata = await _fetch_oidc_metadata()
        auth_endpoint = str(metadata.get("authorization_endpoint", "")).strip()
        token_endpoint = str(metadata.get("token_endpoint", "")).strip()
        if not auth_endpoint or not token_endpoint:
            raise RuntimeError("Discovery OIDC sem authorization_endpoint/token_endpoint.")

        code_verifier, code_challenge = _build_pkce_pair()
        state = secrets.token_urlsafe(24)
        auth_params = {
            "client_id": OIDC_CLIENT_ID,
            "redirect_uri": OIDC_REDIRECT_URI,
            "response_type": "code",
            "scope": OIDC_SCOPE,
            "state": state,
            "response_mode": "query",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        if OIDC_IDP_HINT:
            auth_params["kc_idp_hint"] = OIDC_IDP_HINT

        auth_url = f"{auth_endpoint}?{urlencode(auth_params)}"

        if _is_local_redirect_uri(OIDC_REDIRECT_URI):
            try:
                callback_data = await asyncio.to_thread(_wait_for_oidc_callback, OIDC_REDIRECT_URI, auth_url, state)
            except Exception as exc:
                if verbose:
                    print(f"Não foi possível usar o callback local: {exc}")
                callback_data = {}

        if not callback_data:
            print("Abre o navegador para concluir a autenticação federada:")
            print(auth_url)
            try:
                webbrowser.open(auth_url, new=1, autoraise=True)
            except Exception:
                pass
            raw_callback = input("Depois cola a URL completa de retorno (ou apenas o code): ").strip()
            callback_data = _parse_oidc_callback_response(raw_callback)

        if callback_data.get("error"):
            error_description = callback_data.get("error_description") or callback_data.get("error")
            raise RuntimeError(f"OIDC rejeitado: {error_description}")

        code = callback_data.get("code", "").strip()
        returned_state = callback_data.get("state", "").strip()
        if not code:
            raise RuntimeError("Resposta OIDC sem code.")
        if returned_state and returned_state != state:
            raise RuntimeError("State OIDC inválido.")

        token_data = await _exchange_oidc_code(token_endpoint, code, code_verifier)
        access_token = str(token_data.get("access_token", "")).strip()
        id_token = str(token_data.get("id_token", "")).strip()
        if not access_token:
            raise RuntimeError("Token OIDC sem access_token.")

        codigo = await _resolve_oidc_username(metadata, access_token, id_token)
        if not codigo:
            raise RuntimeError("Token OIDC sem username utilizável.")

        result = await session.call_tool("login_oidc", arguments={"access_token": access_token, "codigo": codigo})
        response = result.content[0].text if result.content else ""
        if verbose:
            print(response)
        return "bem-sucedido" in response.lower()
    except Exception as exc:
        if verbose:
            print(f"Erro ao autenticar federado: {exc}")
        return False


def _question_needs_auth(question: str) -> bool:
    q = question.lower()
    patterns = [
        "horário da uc",
        "horario da uc",
        "horário da disciplina",
        "horario da disciplina",
        "horário da cadeira",
        "horario da cadeira",
        "horário da unidade curricular",
        "horario da unidade curricular",
        "meu horário",
        "meus exames",
        "exames inscritos",
        "meu perfil",
        "meus dados",
        "minhas notas",
        "minhas inscrições",
        "inscrições",
        "inscricoes",
        "conta corrente",
        "saldo vencido",
        "saldo em dívida",
        "saldo em divida",
        "devo",
        "quanto devo",
        "current account",
        "outstanding balance",
    ]
    return any(pattern in q for pattern in patterns)


async def _get_student_code(
    session: ClientSession,
    is_authenticated: bool,
    call_tool_fn=None,
) -> str | None:
    if not is_authenticated:
        return None
    try:
        call_tool = call_tool_fn or session.call_tool
        result = await call_tool("get_session_status", arguments={})
        status = result.content[0].text if result.content else ""
        match = re.search(r"\((\d+)\)", status)
        return match.group(1) if match else None
    except Exception:
        return None


def _choose_source_url(intents: dict, context_type: str | None, student_code: str | None) -> str:
    """Escolhe uma única URL de confirmação com prioridade por intenção."""
    priority = [
        "teacher",
        "course_schedule_mobile",
        "course",
        "schedule",
        "exams",
        "profile",
        "grades",
        "enrollments",
        "current_account",
        "canteen",
        "parking",
        "calendar",
    ]
    chosen_intent = None

    for key in priority:
        if intents.get(key):
            chosen_intent = key
            break

    if chosen_intent:
        base_url = SOURCE_URLS[chosen_intent]
        if student_code and chosen_intent == "current_account":
            return _append_query_params(base_url, {"pct_cod": student_code})
        if student_code and chosen_intent in AUTH_INTENTS:
            return _append_query_params(base_url, {"pv_codigo": student_code})
        return base_url

    if context_type:
        base_url = SOURCE_URLS_BY_CONTEXT.get(context_type, DEFAULT_SOURCE_URL)
        if student_code and context_type == "conta corrente":
            return _append_query_params(base_url, {"pct_cod": student_code})
        if student_code and context_type in AUTH_CONTEXT_LABELS:
            return _append_query_params(base_url, {"pv_codigo": student_code})
        return base_url
    return DEFAULT_SOURCE_URL


# ---------------------------------------------------------------------------
# Lógica principal
# ---------------------------------------------------------------------------
async def _call_auth_tool(
    session: ClientSession,
    tool_name: str,
    context_label: str,
    is_authenticated: bool,
    verbose: bool,
    call_tool_fn=None,
) -> str:
    """Função auxiliar para chamar ferramentas que requerem login e processar logs visuais."""
    if not is_authenticated:
        return f"\n[NOTA: Dados de {context_label} requerem login no SIGARRA.]"
    
    if verbose:
        print(f"  [MCP] A obter {context_label}...", end=" ", flush=True)
        
    call_tool = call_tool_fn or session.call_tool
    result = await call_tool(tool_name, arguments={})
    data = result.content[0].text if result.content else ""
    
    if verbose:
        print("OK")
        
    global _conversation_context
    _conversation_context = {"type": context_label, "data": data}
    return f"\nDados ({context_label}):\n{data}"


async def ask(question: str, session: ClientSession, is_authenticated: bool = False, verbose: bool = True) -> str:
    global _conversation_context
    global _last_ask_meta
    current_account_summary = ""
    direct_answer = None
    tools_called_ordered = []

    async def call_mcp_tool(tool_name: str, arguments: dict):
        tools_called_ordered.append(tool_name)
        return await session.call_tool(tool_name, arguments=arguments)
    
    if verbose:
        tools_result = await session.list_tools()
        print(f"  [MCP] Ferramentas disponíveis: {[t.name for t in tools_result.tools]}")

    # Obter data atual sempre
    date_result = await call_mcp_tool("get_current_date", {})
    context_parts = [f"Data de hoje: {date_result.content[0].text if date_result.content else ''}"]
    
    question_lower = question.lower()
    
    # 1. Agrupamento e deteção de intenções
    intents = {
        "teacher": any(kw in question_lower for kw in ["professor", "docente", "email", "gabinete", "contacto"]),
        "course": any(kw in question_lower for kw in ["uc", "unidade curricular", "disciplina", "cadeira", "programa", "avaliação", "avaliacao", "objetivos", "objectivos"]),
        "calendar": any(kw in question_lower for kw in ["calendário", "semestre", "férias", "feriado", "época", "exames", "exame"]),
        "canteen": any(kw in question_lower for kw in ["cantina", "menu", "ementa", "almoço", "jantar"]),
        "parking": any(kw in question_lower for kw in ["parque", "estacionamento", "parking", "lugares"]),
        "schedule": any(kw in question_lower for kw in ["horário", "horario", "aulas", "meu horário"]),
        "exams": any(kw in question_lower for kw in ["meus exames", "exames inscritos", "quando tenho exame", "minha prova"]),
        "profile": any(kw in question_lower for kw in ["meu perfil", "meus dados", "meu curso", "meu número","média","media"]),
        "grades": any(kw in question_lower for kw in ["minhas notas", "minha nota", "nota", "notas"]),
        "enrollments": any(kw in question_lower for kw in ["minhas inscrições", "inscrições", "inscritos", "uc inscritas"]),
        "current_account": any(
            kw in question_lower
            for kw in [
                "conta corrente",
                "saldo vencido",
                "saldo em divida",
                "saldo em dívida",
                "divida",
                "dívida",
                "devo",
                "quanto devo",
                "debt",
                "overdue",
                "outstanding balance",
                "current account",
            ]
        ),
    }
    intents["course_schedule_mobile"] = any(
        phrase in question_lower
        for phrase in [
            "horário da uc",
            "horario da uc",
            "horário da disciplina",
            "horario da disciplina",
            "horário da cadeira",
            "horario da cadeira",
            "horário da unidade curricular",
            "horario da unidade curricular",
        ]
    ) or (
        any(marker in question_lower for marker in ["horário", "horario"])
        and any(marker in question_lower for marker in ["uc", "unidade curricular", "disciplina", "cadeira"])
    )
    intents["schedule"] = intents["schedule"] and not intents["course_schedule_mobile"]
    has_new_intent = any(intents.values())
    intents["follow_up"] = _is_follow_up_question(question_lower, has_new_intent) and _conversation_context["type"]
    source_url_override = None

    # 2. Processamento do Follow-Up
    if intents["follow_up"] and _conversation_context["data"]:
        if verbose: print(f"  [MCP] Follow-up detectado sobre {_conversation_context['type']}...")
        ctx_data = _conversation_context["data"]
        if len(ctx_data) > 2000:
            ctx_data = ctx_data[:2000] + "\n[... mais dados truncados ...]"
        context_parts.append(f"\n--- Contexto anterior ({_conversation_context['type']}) ---\n{ctx_data}")

    # 3. Execução das Ferramentas Privadas
    if intents["schedule"]:
        context_parts.append(
            await _call_auth_tool(
                session,
                "get_my_schedule",
                "horário",
                is_authenticated,
                verbose,
                call_tool_fn=call_mcp_tool,
            )
        )
    if intents["exams"]:
        context_parts.append(
            await _call_auth_tool(
                session,
                "get_my_exams",
                "exames",
                is_authenticated,
                verbose,
                call_tool_fn=call_mcp_tool,
            )
        )
    if intents["profile"]:
        context_parts.append(
            await _call_auth_tool(
                session,
                "get_my_profile",
                "perfil",
                is_authenticated,
                verbose,
                call_tool_fn=call_mcp_tool,
            )
        )
    if intents["grades"]:
        context_parts.append(
            await _call_auth_tool(
                session,
                "get_my_grades",
                "notas",
                is_authenticated,
                verbose,
                call_tool_fn=call_mcp_tool,
            )
        )
    if intents["enrollments"]:
        context_parts.append(
            await _call_auth_tool(
                session,
                "get_my_enrollments",
                "inscrições",
                is_authenticated,
                verbose,
                call_tool_fn=call_mcp_tool,
            )
        )
    if intents["current_account"]:
        account_block = await _call_auth_tool(
            session,
            "get_my_current_account",
            "conta corrente",
            is_authenticated,
            verbose,
            call_tool_fn=call_mcp_tool,
        )
        context_parts.append(account_block)
        marker = "\nDados (conta corrente):\n"
        if marker in account_block:
            current_account_summary = account_block.split(marker, 1)[1].strip()

    # 4. Execução das Ferramentas Públicas
    if intents["teacher"]:
        if verbose: print("  [MCP] A pesquisar docentes...", end=" ", flush=True)
        match = re.search(r'(?:professor|docente|prof\.?)\s+([a-zà-ú]+(?:\s+[a-zà-ú]+)*)', question_lower)
        if match:
            search_res = await call_mcp_tool("search_teachers", {"nome": match.group(1)})
            data = search_res.content[0].text if search_res.content else ""
            context_parts.append(f"\nPesquisa de docentes:\n{data}")
            _conversation_context = {"type": "docentes", "data": data}
            
            for i, codigo in enumerate(re.findall(r'código:\s*(\d+)', data)[:2]):
                prof_res = await call_mcp_tool("get_teacher_profile", {"codigo": int(codigo)})
                context_parts.append(f"\nPerfil {i+1}:\n{prof_res.content[0].text if prof_res.content else ''}")
        if verbose: print("OK")

    if intents["course_schedule_mobile"]:
        if verbose: print("  [MCP] A obter horário da UC (móvel)...", end=" ", flush=True)
        course_match = re.search(
            r'(?:uc|unidade curricular|disciplina|cadeira|programa)(?:\s+de|\s+da|\s+do)?\s+([a-zà-ú0-9\-]+(?:\s+[a-zà-ú0-9\-]+)*)',
            question_lower,
        )
        uc_query = course_match.group(1).strip(" .?!") if course_match else question.strip()

        search_res = await call_mcp_tool("search_courses", {"query": uc_query})
        search_data = search_res.content[0].text if search_res.content else ""
        context_parts.append(f"\nResultados da pesquisa de UCs:\n{search_data}")
        _conversation_context = {"type": "horário da uc", "data": search_data}

        found_ids = re.findall(r'ocorrencia_id:\s*(\d+)', search_data)
        if found_ids:
            selected_id = found_ids[0]
            semana_ini, semana_fim = _week_range_strings()
            schedule_res = await call_mcp_tool(
                "get_course_schedule_mobile",
                {
                    "ocorrencia_id": int(selected_id),
                    "semana_ini": semana_ini,
                    "semana_fim": semana_fim,
                },
            )
            schedule_data = schedule_res.content[0].text if schedule_res.content else ""
            context_parts.append(f"\nHorário da UC encontrada:\n{schedule_data}")
            _conversation_context = {"type": "horário da uc", "data": schedule_data}
            if schedule_data:
                direct_answer = schedule_data
            source_url_override = _append_query_params(
                SOURCE_URLS["course_schedule_mobile"],
                {
                    "pv_ocorrencia_id": selected_id,
                    "pv_semana_ini": semana_ini,
                    "pv_semana_fim": semana_fim,
                },
            )
            if len(found_ids) > 1:
                context_parts.append("\nNota: Foram encontradas várias UCs; o horário acima refere-se ao primeiro resultado.")
        else:
            source_url_override = SOURCE_URLS["course_schedule_mobile"]

        if verbose: print("OK")

    if intents["course"] and not intents["course_schedule_mobile"]:
        if verbose: print("  [MCP] A pesquisar UC...", end=" ", flush=True)
        course_match = re.search(
            r'(?:uc|unidade curricular|disciplina|cadeira|programa)(?:\s+de|\s+da|\s+do)?\s+([a-zà-ú0-9\-]+(?:\s+[a-zà-ú0-9\-]+)*)',
            question_lower,
        )
        uc_query = course_match.group(1).strip(" .?!") if course_match else question.strip()

        search_res = await call_mcp_tool("search_courses", {"query": uc_query})
        search_data = search_res.content[0].text if search_res.content else ""
        context_parts.append(f"\nResultados da pesquisa de UCs:\n{search_data}")
        _conversation_context = {"type": "uc", "data": search_data}

        found_ids = re.findall(r'ocorrencia_id:\s*(\d+)', search_data)
        if found_ids:
            selected_id = found_ids[0]
            info_res = await call_mcp_tool("get_course_info", {"ocorrencia_id": int(selected_id)})
            info_data = info_res.content[0].text if info_res.content else ""
            context_parts.append(f"\nFicha da UC encontrada:\n{info_data}")
            _conversation_context = {"type": "uc", "data": info_data}
            source_url_override = _append_query_params(COURSE_INFO_VIEW_URL, {"pv_ocorrencia_id": selected_id})
            if len(found_ids) > 1:
                context_parts.append("\nNota: Foram encontradas várias UCs; os detalhes acima referem-se ao primeiro resultado.")
        else:
            source_url_override = SOURCE_URLS["course"]

        if verbose: print("OK")

    if intents["canteen"]:
        if verbose: print("  [MCP] A obter cantina...", end=" ", flush=True)
        res = await call_mcp_tool("get_canteen_menu", {})
        data = res.content[0].text if res.content else ""
        context_parts.append(f"\nCantinas:\n{data}")
        _conversation_context = {"type": "cantina", "data": data}
        if verbose: print("OK")

    if intents["parking"]:
        if verbose: print("  [MCP] A obter parques...", end=" ", flush=True)
        res = await call_mcp_tool("get_parking_status", {})
        data = res.content[0].text if res.content else ""
        context_parts.append(f"\nParques:\n{data}")
        _conversation_context = {"type": "parques", "data": data}
        if verbose: print("OK")

    if intents["calendar"]:
        if verbose: print("  [MCP] A obter calendário...", end=" ", flush=True)
        res = await call_mcp_tool("get_academic_calendar", {})
        data = res.content[0].text if res.content else ""
        context_parts.append(f"\nCalendário escolar:\n{data[:8000]}")
        _conversation_context = {"type": "calendário", "data": data}
        if verbose: print("OK")

    student_code = await _get_student_code(session, is_authenticated, call_tool_fn=call_mcp_tool)
    source_url = _choose_source_url(intents, _conversation_context["type"], student_code)
    if source_url_override:
        source_url = source_url_override

    if intents.get("course_schedule_mobile") and direct_answer:
        answer_with_source = f"{direct_answer}\n\nFonte: {source_url}"
        _last_ask_meta = {
            "tools_used": list(OrderedDict.fromkeys(tools_called_ordered).keys()),
            "mcp_calls": len(tools_called_ordered),
        }
        if verbose:
            print(f"Resposta: {direct_answer}")
            print(f"\nFonte: {source_url}")
        return answer_with_source

    # 5. Comunicação com a API (LLM)
    response_language = _detect_response_language(question)
    language_instruction = (
        "Answer in English only." if response_language == "en" else "Responde em português europeu."
    )
    enriched_message = (
        f"""{chr(10).join(context_parts)}\n\nPergunta do utilizador: {question}\nResponde com base nos dados fornecidos."""
        f" {language_instruction} Não incluas URLs na resposta."
    )
    
    if verbose: print("Resposta: ", end="", flush=True)
    
    headers = {"Authorization": f"Bearer {API_KEY}", "x-api-key": API_KEY}
    payload = {"message": enriched_message, "thread_id": str(uuid.uuid4()), "channel_id": CHANNEL_ID, "user_info": json.dumps({"id": "user", "name": "Utilizador"})}
    
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", API_ENDPOINT, headers=headers, data=payload) as response:
                if response.status_code != 200:
                    _last_ask_meta = {
                        "tools_used": list(OrderedDict.fromkeys(tools_called_ordered).keys()),
                        "mcp_calls": len(tools_called_ordered),
                    }
                    return f"Erro HTTP {response.status_code} na API do LLM."
                
                full_response = ""
                token_received = False
                async for chunk in response.aiter_text():
                    for line in filter(None, chunk.split("\n")):
                        try:
                            data = json.loads(line)
                            if data.get("type") == "token":
                                token_received = True
                                full_response += data.get("content", "")
                                if verbose: print(data.get("content", ""), end="", flush=True)
                        except json.JSONDecodeError:
                            pass

                answer = full_response.strip()
                if not answer:
                    answer = current_account_summary or "Sem resposta do LLM."
                    if verbose and not token_received:
                        print(answer, end="", flush=True)

                answer_with_source = f"{answer}\n\nFonte: {source_url}"
                _last_ask_meta = {
                    "tools_used": list(OrderedDict.fromkeys(tools_called_ordered).keys()),
                    "mcp_calls": len(tools_called_ordered),
                }
                if verbose:
                    print(f"\n\nFonte: {source_url}")
                return answer_with_source
    except Exception as exc:
        _last_ask_meta = {
            "tools_used": list(OrderedDict.fromkeys(tools_called_ordered).keys()),
            "mcp_calls": len(tools_called_ordered),
        }
        return f"Erro ao comunicar com a API: {exc}"


def _banner() -> None:
    print("=" * 60)
    print("  Assistente SIGARRA — FEUP (MCP Server + LLM)")
    print("=" * 60)


async def do_login(session: ClientSession) -> bool:
    print("\n" + "-" * 40 + "\n  LOGIN SIGARRA\n" + "-" * 40)
    if _oidc_enabled():
        choice = input("Entrar com autenticação federada OIDC? [S/n/c=clássico]: ").strip().lower()
        if choice not in {"c", "classic", "clássico", "classico"}:
            if await _login_oidc(session):
                return True
            fallback = input("Autenticação federada falhou. Queres tentar login SIGARRA clássico? [S/n]: ").strip().lower()
            if fallback in {"n", "nao", "não", "no"}:
                return False

    username = input("Username (ex: up123456789) [Enter para saltar]: ").strip()
    if not username: return False
    
    password = get_password("Password: ")
    if not password: return False
    
    print("\nA autenticar...", end=" ", flush=True)
    result = await session.call_tool("login", arguments={"username": username, "password": password})
    response = result.content[0].text if result.content else ""
    print(f"\n{response}")
    return "bem-sucedido" in response.lower()


async def interactive_mode() -> None:
    _banner()
    server_params = StdioServerParameters(command=sys.executable, args=[str(SERVER_SCRIPT)])
    
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            is_authenticated = await do_login(session)
            
            print("\nEscreva a sua pergunta ou 'sair' para terminar.\n")
            while True:
                try:
                    question = input("\nPergunta: ").strip()
                except (EOFError, KeyboardInterrupt):
                    break

                if not question: continue
                if question.lower() in {"sair", "exit", "quit"}:
                    if is_authenticated:
                        await session.call_tool("logout", arguments={})
                    break

                print()
                await ask(question, session, is_authenticated)


async def single_question_mode(question: str) -> None:
    _banner()
    print(f"\nPergunta: {question}\n")
    server_params = StdioServerParameters(command=sys.executable, args=[str(SERVER_SCRIPT)])
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            is_authenticated = False
            if _question_needs_auth(question):
                is_authenticated = await _login_oidc(session, verbose=True)
            await ask(question, session, is_authenticated=is_authenticated)


def main() -> None:
    if not API_KEY:
        print("Erro: API_KEY em falta no ficheiro .env")
        sys.exit(1)

    if len(sys.argv) > 1:
        asyncio.run(single_question_mode(" ".join(sys.argv[1:])))
    else:
        asyncio.run(interactive_mode())


if __name__ == "__main__":
    main()
