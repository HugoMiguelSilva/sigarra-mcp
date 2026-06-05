#!/usr/bin/env python3
"""
Aplicacao web (UI user-friendly) para o assistente SIGARRA.
"""

import asyncio
import html
import base64
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
import urllib.parse

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import client

# Carrega variaveis de ambiente uma unica vez.
load_dotenv()

BASE_DIR = Path(__file__).parent
SERVER_SCRIPT = BASE_DIR / "server.py"
WEB_DIR = BASE_DIR / "web"


class ChatRequest(BaseModel):
    message: str
    conversation_id: int | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateConversationRequest(BaseModel):
    title: str | None = None


class UpdateConversationTitleRequest(BaseModel):
    title: str


class MessageFeedbackRequest(BaseModel):
    feedback: str


class AppState:
    def __init__(self) -> None:
        self.session: ClientSession | None = None
        self._read = None
        self._write = None
        self._stdio_cm = None
        self._session_cm = None
        self.db: sqlite3.Connection | None = None
        self.mcp_lock = asyncio.Lock()
        self.db_lock = asyncio.Lock()


state = AppState()
BROWSER_COOKIE_NAME = "sigarra_ui_id"
ADMIN_COOKIE_NAME = "admin_session"
DB_PATH = BASE_DIR / "sigarra_ui.db"
OIDC_AUTH_ENDPOINT = "https://open-id.up.pt/realms/sigarra/protocol/openid-connect/auth"
OIDC_TOKEN_ENDPOINT = "https://open-id.up.pt/realms/sigarra/protocol/openid-connect/token"

_OIDC_STATES: dict[str, dict[str, str | float]] = {}
_OIDC_STATES_LOCK = threading.Lock()


def _oidc_config() -> dict[str, str]:
    return {
        "client_id": os.environ.get("OIDC_CLIENT_ID", "").strip(),
        "client_secret": os.environ.get("OIDC_CLIENT_SECRET", "").strip(),
        "redirect_uri": os.environ.get("OIDC_REDIRECT_URI", "").strip(),
        "auth_endpoint": OIDC_AUTH_ENDPOINT,
        "token_endpoint": OIDC_TOKEN_ENDPOINT,
    }


def _oidc_enabled() -> bool:
    cfg = _oidc_config()
    return bool(cfg["client_id"] and cfg["client_secret"] and cfg["redirect_uri"])


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


def _oidc_error_page(title: str, message: str) -> HTMLResponse:
        page = f"""<!doctype html>
<html lang="pt">
    <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{html.escape(title)}</title>
        <style>
            body {{ font-family: system-ui, sans-serif; margin: 0; padding: 2rem; background: #f4f7fb; color: #1f2937; }}
            .card {{ max-width: 42rem; margin: 8vh auto 0; background: white; border-radius: 16px; padding: 1.5rem; box-shadow: 0 12px 40px rgba(15, 23, 42, 0.12); }}
            .muted {{ color: #6b7280; }}
            a {{ color: #0f766e; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h3>{html.escape(title)}</h3>
            <p>{html.escape(message)}</p>
            <p class="muted">Se a autenticação federada falhar, usa o login SIGARRA abaixo.</p>
            <p><a href="/">Voltar à aplicação</a></p>
        </div>
    </body>
</html>"""
        return HTMLResponse(page, status_code=200)


def _extract_source_link(answer: str) -> tuple[str, str | None]:
    """Separa a resposta do sufixo 'Fonte: <url>' e devolve link clicavel."""
    match = re.search(r"\n\nFonte:\s*(https?://\S+)\s*$", answer.strip())
    if not match:
        return answer.strip(), None

    source_url = match.group(1)
    clean_answer = answer[: match.start()].strip()
    return clean_answer, source_url


def _get_admin_auth_status(request: Request) -> bool:
    """Verifica se o utilizador tem sessão admin válida."""
    return request.cookies.get(ADMIN_COOKIE_NAME) == "authenticated"


def _utc_now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _is_status_authenticated(status_text: str) -> bool:
    status_lower = status_text.lower()
    return "sessão activa" in status_lower or "sessao activa" in status_lower


def _extract_account_scope(status_text: str, fallback_login: str | None = None) -> str:
    """Extrai um identificador estavel da conta autenticada para isolar conversas."""
    if not _is_status_authenticated(status_text):
        return "anon"

    match = re.search(r"\((\d+)\)", status_text)
    if match:
        return f"sigarra:{match.group(1)}"

    if fallback_login:
        return f"login:{fallback_login.lower()}"

    return "authenticated"


def _infer_conversation_title(message: str) -> str:
    cleaned = re.sub(r"\s+", " ", message).strip(" .?!")
    if not cleaned:
        return "Nova conversa"
    if len(cleaned) <= 60:
        return cleaned
    return f"{cleaned[:57].rstrip()}..."


def _init_db(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            browser_id TEXT NOT NULL,
            account_scope TEXT NOT NULL DEFAULT 'anon',
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            text TEXT NOT NULL,
            source_url TEXT,
            response_ms REAL,
            tools_used TEXT,
            mcp_calls INTEGER,
            is_supported INTEGER,
            feedback INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY(conversation_id) REFERENCES conversations(id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS browser_sessions (
            browser_id TEXT PRIMARY KEY,
            authenticated INTEGER NOT NULL DEFAULT 0,
            account_scope TEXT NOT NULL DEFAULT 'anon'
        )
        """
    )

    # Migracao leve para bases de dados antigas sem as novas colunas.
    conv_cols = {
        row[1] for row in connection.execute("PRAGMA table_info(conversations)").fetchall()
    }
    if "account_scope" not in conv_cols:
        connection.execute(
            "ALTER TABLE conversations ADD COLUMN account_scope TEXT NOT NULL DEFAULT 'anon'"
        )

    browser_cols = {
        row[1] for row in connection.execute("PRAGMA table_info(browser_sessions)").fetchall()
    }
    if "account_scope" not in browser_cols:
        connection.execute(
            "ALTER TABLE browser_sessions ADD COLUMN account_scope TEXT NOT NULL DEFAULT 'anon'"
        )

    msg_cols = {
        row[1] for row in connection.execute("PRAGMA table_info(messages)").fetchall()
    }
    if "response_ms" not in msg_cols:
        connection.execute("ALTER TABLE messages ADD COLUMN response_ms REAL")
    if "tools_used" not in msg_cols:
        connection.execute("ALTER TABLE messages ADD COLUMN tools_used TEXT")
    if "mcp_calls" not in msg_cols:
        connection.execute("ALTER TABLE messages ADD COLUMN mcp_calls INTEGER")
    if "is_supported" not in msg_cols:
        connection.execute("ALTER TABLE messages ADD COLUMN is_supported INTEGER")
    if "feedback" not in msg_cols:
        connection.execute("ALTER TABLE messages ADD COLUMN feedback INTEGER")

    connection.commit()


def _get_or_create_browser_id(request: Request, response: Response) -> str:
    browser_id = request.cookies.get(BROWSER_COOKIE_NAME)
    if browser_id:
        return browser_id

    browser_id = uuid.uuid4().hex
    response.set_cookie(
        key=BROWSER_COOKIE_NAME,
        value=browser_id,
        max_age=60 * 60 * 24 * 180,
        httponly=True,
        samesite="lax",
    )
    return browser_id


def _db_ensure_browser(browser_id: str) -> None:
    state.db.execute(
        "INSERT OR IGNORE INTO browser_sessions(browser_id, authenticated, account_scope) VALUES(?, 0, 'anon')",
        (browser_id,),
    )
    state.db.commit()


def _db_set_browser_session(browser_id: str, authenticated: bool, account_scope: str) -> None:
    _db_ensure_browser(browser_id)
    state.db.execute(
        "UPDATE browser_sessions SET authenticated = ?, account_scope = ? WHERE browser_id = ?",
        (1 if authenticated else 0, account_scope, browser_id),
    )
    state.db.commit()


def _db_get_authenticated(browser_id: str) -> bool:
    _db_ensure_browser(browser_id)
    row = state.db.execute(
        "SELECT authenticated FROM browser_sessions WHERE browser_id = ?",
        (browser_id,),
    ).fetchone()
    return bool(row[0]) if row else False


def _db_get_account_scope(browser_id: str) -> str:
    _db_ensure_browser(browser_id)
    row = state.db.execute(
        "SELECT account_scope FROM browser_sessions WHERE browser_id = ?",
        (browser_id,),
    ).fetchone()
    if not row or not row[0]:
        return "anon"
    return str(row[0])


def _db_create_conversation(browser_id: str, account_scope: str, title: str) -> int:
    now = _utc_now()
    cursor = state.db.execute(
        "INSERT INTO conversations(browser_id, account_scope, title, created_at, updated_at) VALUES(?, ?, ?, ?, ?)",
        (browser_id, account_scope, title, now, now),
    )
    state.db.commit()
    return int(cursor.lastrowid)


def _db_conversation_exists(browser_id: str, account_scope: str, conversation_id: int) -> bool:
    row = state.db.execute(
        "SELECT id FROM conversations WHERE id = ? AND browser_id = ? AND account_scope = ?",
        (conversation_id, browser_id, account_scope),
    ).fetchone()
    return row is not None


def _db_add_message(
    conversation_id: int,
    role: str,
    text: str,
    source_url: str | None = None,
    response_ms: float | None = None,
    tools_used: list[str] | None = None,
    mcp_calls: int | None = None,
    is_supported: bool | None = None,
) -> int:
    now = _utc_now()
    cursor = state.db.execute(
        """
        INSERT INTO messages(
            conversation_id, role, text, source_url, response_ms, tools_used, mcp_calls, is_supported, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            conversation_id,
            role,
            text,
            source_url,
            response_ms,
            json.dumps(tools_used or []),
            mcp_calls,
            None if is_supported is None else (1 if is_supported else 0),
            now,
        ),
    )
    state.db.execute(
        "UPDATE conversations SET updated_at = ? WHERE id = ?",
        (now, conversation_id),
    )
    state.db.commit()
    return int(cursor.lastrowid)


def _infer_supported_answer(answer: str) -> bool:
    text = (answer or "").lower()
    unsupported_markers = [
        "nao consigo",
        "não consigo",
        "nao suport",
        "não suport",
        "nao foi possivel",
        "não foi possível",
        "not found",
        "erro ao",
        "sem resposta",
    ]
    return not any(marker in text for marker in unsupported_markers)


def _infer_tools_from_message(source_url: str | None, answer_text: str) -> list[str]:
    """Heurística para mensagens antigas sem campo tools_used preenchido."""
    inferred = ["get_current_date"]
    url = (source_url or "").lower()
    text = (answer_text or "").lower()

    mapping = [
        ("search_teachers", ["mob_func_geral", "docente", "professor", "gabinete", "email"]),
        ("search_courses", ["ucurr_geral", "ocorrencia", "unidade curricular", "disciplina"]),
        ("get_course_info", ["ficha_uc_view", "objetivos", "avaliação", "avaliacao"]),
        ("get_parking_status", ["ocupacao_parques", "p1", "p3", "p4", "estacionamento"]),
        ("get_canteen_menu", ["cantinas", "ementa", "menu"]),
        ("get_academic_calendar", ["calend", "semestre", "feriado", "época"]),
        ("get_my_schedule", ["hor_geral", "horário", "aulas", "sala"]),
        ("get_my_exams", ["fest_geral.exames", "exames inscritos"]),
        ("get_my_profile", ["percurso_academico", "perfil de estudante"]),
        ("get_my_grades", ["notas", "valores"]),
        ("get_my_enrollments", ["inscricoes_corrente", "inscrições atuais"]),
        ("get_my_current_account", ["ccorrente", "conta corrente", "saldo vencido", "dívida", "divida"]),
    ]

    for tool_name, hints in mapping:
        if any(hint in url for hint in hints) or any(hint in text for hint in hints):
            inferred.append(tool_name)

    # Remove duplicados preservando ordem.
    return list(dict.fromkeys(inferred))


def _db_set_message_feedback(
    browser_id: str,
    account_scope: str,
    message_id: int,
    feedback: int,
) -> bool:
    row = state.db.execute(
        """
        SELECT m.id
        FROM messages m
        JOIN conversations c ON c.id = m.conversation_id
        WHERE m.id = ?
          AND m.role = 'assistant'
          AND c.browser_id = ?
          AND c.account_scope = ?
        """,
        (message_id, browser_id, account_scope),
    ).fetchone()
    if not row:
        return False

    state.db.execute(
        "UPDATE messages SET feedback = ? WHERE id = ?",
        (feedback, message_id),
    )
    state.db.commit()
    return True


def _bucket_expr(granularity: str) -> str:
    if granularity == "week":
        return "strftime('%Y-W%W', created_at)"
    if granularity == "month":
        return "substr(created_at, 1, 7)"
    return "substr(created_at, 1, 10)"


def _build_admin_stats(granularity: str) -> dict:
    bucket_expr = _bucket_expr(granularity)

    total_conversations = int(state.db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0])
    total_questions = int(state.db.execute("SELECT COUNT(*) FROM messages WHERE role = 'user'").fetchone()[0])
    total_answers = int(state.db.execute("SELECT COUNT(*) FROM messages WHERE role = 'assistant'").fetchone()[0])

    distinct_users = int(
        state.db.execute(
            """
            SELECT COUNT(DISTINCT CASE
                WHEN account_scope <> 'anon' THEN account_scope
                ELSE 'anon:' || browser_id
            END)
            FROM conversations
            """
        ).fetchone()[0]
    )

    perf_rows = state.db.execute(
        "SELECT response_ms FROM messages WHERE role = 'assistant' AND response_ms IS NOT NULL ORDER BY response_ms"
    ).fetchall()
    perf_values = [float(row[0]) for row in perf_rows]
    avg_ms = round(sum(perf_values) / len(perf_values), 2) if perf_values else 0.0
    p95_ms = round(perf_values[int(len(perf_values) * 0.95) - 1], 2) if perf_values else 0.0
    max_ms = round(max(perf_values), 2) if perf_values else 0.0

    db_size_bytes = DB_PATH.stat().st_size if DB_PATH.exists() else 0

    timeline_rows = state.db.execute(
        f"""
        SELECT {bucket_expr} AS bucket,
               SUM(CASE WHEN role = 'user' THEN 1 ELSE 0 END) AS questions,
               SUM(CASE WHEN role = 'assistant' THEN 1 ELSE 0 END) AS answers
        FROM messages
        GROUP BY bucket
        ORDER BY bucket DESC
        LIMIT 20
        """
    ).fetchall()
    timeline = [
        {"bucket": row[0], "questions": int(row[1] or 0), "answers": int(row[2] or 0)}
        for row in timeline_rows
    ][::-1]

    conv_timeline_rows = state.db.execute(
        f"""
        SELECT {bucket_expr} AS bucket, COUNT(*)
        FROM conversations
        GROUP BY bucket
        ORDER BY bucket DESC
        LIMIT 20
        """
    ).fetchall()
    conversations_timeline = [
        {"bucket": row[0], "conversations": int(row[1] or 0)} for row in conv_timeline_rows
    ][::-1]

    tool_counts: dict[str, int] = {}
    tool_rows = state.db.execute(
        "SELECT tools_used, source_url, text FROM messages WHERE role = 'assistant'"
    ).fetchall()
    for row in tool_rows:
        raw = row[0] or "[]"
        source_url = row[1]
        answer_text = row[2] or ""
        try:
            tools = json.loads(raw)
        except Exception:
            tools = []
        if not tools:
            tools = _infer_tools_from_message(source_url, answer_text)
        for tool in tools:
            name = str(tool)
            tool_counts[name] = tool_counts.get(name, 0) + 1
    tools_used = [
        {"tool": k, "count": v} for k, v in sorted(tool_counts.items(), key=lambda x: x[1], reverse=True)
    ]

    unsupported_rows = state.db.execute(
        """
        SELECT m.id, m.text, m.created_at, c.id, m.is_supported
        FROM messages m
        JOIN conversations c ON c.id = m.conversation_id
        WHERE m.role = 'assistant'
        ORDER BY m.id DESC
        LIMIT 300
        """
    ).fetchall()
    unsupported = []
    for msg_id, ans_text, created_at, conv_id, is_supported in unsupported_rows:
        supported_flag = None if is_supported is None else bool(is_supported)
        if supported_flag is True:
            continue
        if supported_flag is None and _infer_supported_answer(ans_text):
            continue

        q_row = state.db.execute(
            """
            SELECT text FROM messages
            WHERE conversation_id = ? AND role = 'user' AND id < ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (conv_id, msg_id),
        ).fetchone()
        unsupported.append(
            {
                "message_id": int(msg_id),
                "question": q_row[0] if q_row else "(sem pergunta anterior)",
                "answer": ans_text,
                "created_at": created_at,
            }
        )
        if len(unsupported) >= 30:
            break

    dislike_rows = state.db.execute(
        """
        SELECT m.id, m.text, m.created_at, c.id
        FROM messages m
        JOIN conversations c ON c.id = m.conversation_id
        WHERE m.role = 'assistant' AND m.feedback = -1
        ORDER BY m.id DESC
        LIMIT 30
        """
    ).fetchall()
    dislikes = []
    for msg_id, ans_text, created_at, conv_id in dislike_rows:
        q_row = state.db.execute(
            """
            SELECT text FROM messages
            WHERE conversation_id = ? AND role = 'user' AND id < ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (conv_id, msg_id),
        ).fetchone()
        dislikes.append(
            {
                "message_id": int(msg_id),
                "question": q_row[0] if q_row else "(sem pergunta anterior)",
                "answer": ans_text,
                "created_at": created_at,
            }
        )

    return {
        "totals": {
            "conversations": total_conversations,
            "questions": total_questions,
            "answers": total_answers,
            "users": distinct_users,
        },
        "performance": {
            "avg_response_ms": avg_ms,
            "p95_response_ms": p95_ms,
            "max_response_ms": max_ms,
        },
        "storage": {
            "db_size_bytes": int(db_size_bytes),
            "db_size_mb": round(db_size_bytes / (1024 * 1024), 2),
        },
        "timeline": timeline,
        "conversations_timeline": conversations_timeline,
        "tools_used": tools_used,
        "unsupported_questions": unsupported,
        "disliked_answers": dislikes,
    }


def _db_update_conversation_title(conversation_id: int, title: str) -> None:
    now = _utc_now()
    state.db.execute(
        "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
        (title, now, conversation_id),
    )
    state.db.commit()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if not os.getenv("API_KEY"):
        raise RuntimeError("API_KEY em falta no ficheiro .env")

    server_params = StdioServerParameters(command=sys.executable, args=[str(SERVER_SCRIPT)])

    state._stdio_cm = stdio_client(server_params)
    state._read, state._write = await state._stdio_cm.__aenter__()

    state._session_cm = ClientSession(state._read, state._write)
    state.session = await state._session_cm.__aenter__()
    await state.session.initialize()
    state.db = sqlite3.connect(DB_PATH, check_same_thread=False)
    _init_db(state.db)

    try:
        yield
    finally:
        if state._session_cm is not None:
            await state._session_cm.__aexit__(None, None, None)
        if state._stdio_cm is not None:
            await state._stdio_cm.__aexit__(None, None, None)
        if state.db is not None:
            state.db.close()


app = FastAPI(title="SIGARRA Assistant UI", lifespan=lifespan)
app.mount("/web", StaticFiles(directory=str(WEB_DIR)), name="web")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    index_path = WEB_DIR / "index.html"
    response = HTMLResponse(index_path.read_text(encoding="utf-8"))
    _get_or_create_browser_id(request, response)
    return response


@app.get("/login/oidc")
async def oidc_login_start(request: Request):
    cfg = _oidc_config()
    if not _oidc_enabled():
        return _oidc_error_page(
            "Autenticação federada indisponível",
            "Faltam OIDC_CLIENT_ID, OIDC_CLIENT_SECRET ou OIDC_REDIRECT_URI no ambiente.",
        )

    browser_response = Response(status_code=302)
    browser_id = _get_or_create_browser_id(request, browser_response)
    state_token = secrets.token_urlsafe(24)
    with _OIDC_STATES_LOCK:
        _OIDC_STATES[state_token] = {
            "browser_id": browser_id,
            "expires_at": time.time() + 300,
        }

    params = urllib.parse.urlencode(
        {
            "client_id": cfg["client_id"],
            "redirect_uri": cfg["redirect_uri"],
            "response_type": "code",
            "scope": "openid profile email",
            "state": state_token,
            "response_mode": "query",
            "kc_idp_hint": "saml",
        }
    )
    browser_response.headers["Location"] = f"{cfg['auth_endpoint']}?{params}"
    browser_response.headers["Cache-Control"] = "no-store"
    return browser_response


@app.get("/login/oidc/callback")
async def oidc_login_callback(request: Request, response: Response):
    cfg = _oidc_config()
    if not _oidc_enabled():
        return _oidc_error_page(
            "Autenticação federada indisponível",
            "Faltam OIDC_CLIENT_ID, OIDC_CLIENT_SECRET ou OIDC_REDIRECT_URI no ambiente.",
        )

    error = request.query_params.get("error", "").strip()
    if error:
        error_description = request.query_params.get("error_description", "").strip()
        message = error_description or error
        return _oidc_error_page("Autenticação federada falhou", message)

    state_token = request.query_params.get("state", "").strip()
    code = request.query_params.get("code", "").strip()
    if not state_token or not code:
        return _oidc_error_page("Autenticação federada falhou", "Resposta OIDC incompleta.")

    with _OIDC_STATES_LOCK:
        state_entry = _OIDC_STATES.pop(state_token, None)

    if not state_entry:
        return _oidc_error_page("Autenticação federada falhou", "State inválido ou expirado.")
    if float(state_entry.get("expires_at", 0.0)) < time.time():
        return _oidc_error_page("Autenticação federada falhou", "State expirado. Tenta novamente.")

    browser_id = str(state_entry.get("browser_id", "")).strip()
    if not browser_id:
        return _oidc_error_page("Autenticação federada falhou", "Sessão do browser não encontrada.")

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            token_response = await client.post(
                cfg["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "client_id": cfg["client_id"],
                    "client_secret": cfg["client_secret"],
                    "code": code,
                    "redirect_uri": cfg["redirect_uri"],
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            token_response.raise_for_status()
            token_data = token_response.json()
    except Exception as exc:
        return _oidc_error_page("Autenticação federada falhou", f"Não foi possível trocar o código por token: {exc}")

    access_token = str(token_data.get("access_token", "")).strip()
    id_token = str(token_data.get("id_token", "")).strip()
    codigo = _extract_oidc_username(id_token) or str(token_data.get("preferred_username", "")).strip()
    if codigo.lower().startswith("up"):
        codigo = codigo[2:]
    if "@" in codigo:
        codigo = codigo.split("@", 1)[0]
    if not access_token or not codigo:
        return _oidc_error_page("Autenticação federada falhou", "Token OIDC inválido ou sem username utilizável.")

    response_text = ""
    status_text = ""
    async with state.mcp_lock:
        result = await state.session.call_tool(
            "login_oidc",
            arguments={"access_token": access_token, "codigo": codigo},
        )
        response_text = result.content[0].text if result.content else ""
        if "bem-sucedido" in response_text.lower():
            status_result = await state.session.call_tool("get_session_status", arguments={})
            status_text = status_result.content[0].text if status_result.content else ""

    is_authenticated = "bem-sucedido" in response_text.lower()
    account_scope = _extract_account_scope(status_text, fallback_login=f"up{codigo}")
    async with state.db_lock:
        _db_set_browser_session(browser_id, is_authenticated, account_scope)

    if is_authenticated:
        redirect_response = RedirectResponse(url=str(request.url_for("index")), status_code=303)
        _get_or_create_browser_id(request, redirect_response)
        return redirect_response

    return _oidc_error_page(
        "Autenticação federada falhou",
        response_text or "O SIGARRA rejeitou a sessão federada. Usa o login antigo abaixo.",
    )


@app.get("/admin", response_class=HTMLResponse)
async def admin_index(request: Request, response: Response) -> HTMLResponse:
    if not _get_admin_auth_status(request):
        # Sem autenticação, retornar página de login
        admin_path = WEB_DIR / "admin_login.html"
        if not admin_path.exists():
            raise HTTPException(status_code=404, detail="Pagina de login de admin nao encontrada")
        return HTMLResponse(admin_path.read_text(encoding="utf-8"))
    
    admin_path = WEB_DIR / "admin.html"
    if not admin_path.exists():
        raise HTTPException(status_code=404, detail="Pagina de admin nao encontrada")
    html_response = HTMLResponse(admin_path.read_text(encoding="utf-8"))
    _get_or_create_browser_id(request, html_response)
    return html_response


@app.get("/api/session")
async def session_status(request: Request, response: Response) -> dict:
    if state.session is None:
        raise HTTPException(status_code=503, detail="Sessao MCP indisponivel")
    if state.db is None:
        raise HTTPException(status_code=503, detail="Base de dados indisponivel")

    browser_id = _get_or_create_browser_id(request, response)

    async with state.mcp_lock:
        result = await state.session.call_tool("get_session_status", arguments={})
        status_text = result.content[0].text if result.content else ""

    async with state.db_lock:
        ui_authenticated = _db_get_authenticated(browser_id)
        real_authenticated = _is_status_authenticated(status_text)
        real_scope = _extract_account_scope(status_text)
        if ui_authenticated != real_authenticated or _db_get_account_scope(browser_id) != real_scope:
            _db_set_browser_session(browser_id, real_authenticated, real_scope)

    return {
        "authenticated": real_authenticated,
        "status": status_text,
    }


@app.post("/api/admin/login")
async def admin_login(payload: LoginRequest, request: Request, response: Response) -> dict:
    """Valida password de admin e cria sessão autenticada."""
    admin_password = os.getenv("ADMIN_PASSWORD", "").strip()
    if not admin_password:
        raise HTTPException(status_code=500, detail="Admin password nao configurada")
    
    provided_password = (payload.password or "").strip()
    if provided_password != admin_password:
        raise HTTPException(status_code=401, detail="Password incorreta")
    
    response.set_cookie(
        key=ADMIN_COOKIE_NAME,
        value="authenticated",
        max_age=60 * 60 * 24,  # 24 horas
        httponly=True,
        samesite="lax",
    )
    return {"ok": True, "message": "Admin autenticado com sucesso"}


@app.post("/api/admin/logout")
async def admin_logout(request: Request, response: Response) -> dict:
    """Remove sessão de admin."""
    response.delete_cookie(
        key=ADMIN_COOKIE_NAME,
        samesite="lax",
    )
    return {"ok": True, "message": "Admin logout bem-sucedido"}


@app.post("/api/login")
async def login(payload: LoginRequest, request: Request, response: Response) -> dict:
    if state.session is None:
        raise HTTPException(status_code=503, detail="Sessao MCP indisponivel")
    if state.db is None:
        raise HTTPException(status_code=503, detail="Base de dados indisponivel")

    browser_id = _get_or_create_browser_id(request, response)

    username = payload.username.strip()
    password = payload.password
    if not username or not password:
        raise HTTPException(status_code=400, detail="Username e password sao obrigatorios")

    async with state.mcp_lock:
        result = await state.session.call_tool(
            "login",
            arguments={"username": username, "password": password},
        )
        response_text = result.content[0].text if result.content else ""
        status_text = ""
        if "bem-sucedido" in response_text.lower():
            status_result = await state.session.call_tool("get_session_status", arguments={})
            status_text = status_result.content[0].text if status_result.content else ""

    is_authenticated = "bem-sucedido" in response_text.lower()
    account_scope = _extract_account_scope(status_text, fallback_login=username)
    async with state.db_lock:
        _db_set_browser_session(browser_id, is_authenticated, account_scope)

    return {
        "ok": is_authenticated,
        "message": response_text,
    }


@app.post("/api/logout")
async def logout(request: Request, response: Response) -> dict:
    if state.session is None:
        raise HTTPException(status_code=503, detail="Sessao MCP indisponivel")
    if state.db is None:
        raise HTTPException(status_code=503, detail="Base de dados indisponivel")

    browser_id = _get_or_create_browser_id(request, response)

    async with state.mcp_lock:
        result = await state.session.call_tool("logout", arguments={})
        response_text = result.content[0].text if result.content else ""

    async with state.db_lock:
        _db_set_browser_session(browser_id, False, "anon")

    return {
        "ok": True,
        "message": response_text,
    }


@app.get("/api/conversations")
async def list_conversations(request: Request, response: Response) -> dict:
    if state.db is None:
        raise HTTPException(status_code=503, detail="Base de dados indisponivel")

    browser_id = _get_or_create_browser_id(request, response)

    async with state.db_lock:
        _db_ensure_browser(browser_id)
        account_scope = _db_get_account_scope(browser_id)
        rows = state.db.execute(
            "SELECT id, title, updated_at FROM conversations WHERE browser_id = ? AND account_scope = ? ORDER BY updated_at DESC",
            (browser_id, account_scope),
        ).fetchall()

    return {
        "conversations": [
            {"id": int(row[0]), "title": row[1], "updated_at": row[2]} for row in rows
        ]
    }


@app.post("/api/conversations")
async def create_conversation(payload: CreateConversationRequest, request: Request, response: Response) -> dict:
    if state.db is None:
        raise HTTPException(status_code=503, detail="Base de dados indisponivel")

    browser_id = _get_or_create_browser_id(request, response)
    title = (payload.title or "Nova conversa").strip() or "Nova conversa"

    async with state.db_lock:
        _db_ensure_browser(browser_id)
        account_scope = _db_get_account_scope(browser_id)
        conversation_id = _db_create_conversation(browser_id, account_scope, title)

    return {
        "id": conversation_id,
        "title": title,
    }


@app.patch("/api/conversations/{conversation_id}")
async def update_conversation_title(
    conversation_id: int,
    payload: UpdateConversationTitleRequest,
    request: Request,
    response: Response,
) -> dict:
    if state.db is None:
        raise HTTPException(status_code=503, detail="Base de dados indisponivel")

    browser_id = _get_or_create_browser_id(request, response)
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="O titulo nao pode ser vazio")

    async with state.db_lock:
        account_scope = _db_get_account_scope(browser_id)
        if not _db_conversation_exists(browser_id, account_scope, conversation_id):
            raise HTTPException(status_code=404, detail="Conversa nao encontrada")
        _db_update_conversation_title(conversation_id, title)

    return {
        "ok": True,
        "conversation_id": conversation_id,
        "title": title,
    }


@app.get("/api/conversations/{conversation_id}/messages")
async def get_conversation_messages(conversation_id: int, request: Request, response: Response) -> dict:
    if state.db is None:
        raise HTTPException(status_code=503, detail="Base de dados indisponivel")

    browser_id = _get_or_create_browser_id(request, response)

    async with state.db_lock:
        account_scope = _db_get_account_scope(browser_id)
        if not _db_conversation_exists(browser_id, account_scope, conversation_id):
            raise HTTPException(status_code=404, detail="Conversa nao encontrada")

        rows = state.db.execute(
            "SELECT id, role, text, source_url, created_at, feedback FROM messages WHERE conversation_id = ? ORDER BY id ASC",
            (conversation_id,),
        ).fetchall()

    return {
        "messages": [
            {
                "id": int(row[0]),
                "role": row[1],
                "text": row[2],
                "source_url": row[3],
                "created_at": row[4],
                "feedback": row[5],
            }
            for row in rows
        ]
    }


@app.post("/api/messages/{message_id}/feedback")
async def set_message_feedback(message_id: int, payload: MessageFeedbackRequest, request: Request, response: Response) -> dict:
    if state.db is None:
        raise HTTPException(status_code=503, detail="Base de dados indisponivel")

    browser_id = _get_or_create_browser_id(request, response)
    feedback_raw = payload.feedback.strip().lower()
    feedback_value = {"like": 1, "dislike": -1}.get(feedback_raw)
    if feedback_value is None:
        raise HTTPException(status_code=400, detail="Feedback invalido")

    async with state.db_lock:
        account_scope = _db_get_account_scope(browser_id)
        ok = _db_set_message_feedback(browser_id, account_scope, message_id, feedback_value)
    if not ok:
        raise HTTPException(status_code=404, detail="Mensagem nao encontrada")

    return {"ok": True, "message_id": message_id, "feedback": feedback_value}


@app.get("/api/admin/stats")
async def admin_stats(granularity: str = "day") -> dict:
    if state.db is None:
        raise HTTPException(status_code=503, detail="Base de dados indisponivel")

    granularity = granularity.lower().strip()
    if granularity not in {"day", "week", "month"}:
        raise HTTPException(status_code=400, detail="Granularidade invalida")

    async with state.db_lock:
        return _build_admin_stats(granularity)


@app.post("/api/chat")
async def chat(payload: ChatRequest, request: Request, response: Response) -> dict:
    if state.session is None:
        raise HTTPException(status_code=503, detail="Sessao MCP indisponivel")
    if state.db is None:
        raise HTTPException(status_code=503, detail="Base de dados indisponivel")

    browser_id = _get_or_create_browser_id(request, response)

    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="A mensagem nao pode ser vazia")

    conversation_id = payload.conversation_id

    async with state.db_lock:
        _db_ensure_browser(browser_id)
        account_scope = _db_get_account_scope(browser_id)
        if conversation_id is None:
            conversation_id = _db_create_conversation(browser_id, account_scope, _infer_conversation_title(message))
        elif not _db_conversation_exists(browser_id, account_scope, conversation_id):
            raise HTTPException(status_code=404, detail="Conversa nao encontrada")
        _db_add_message(conversation_id, "user", message)

    async with state.db_lock:
        is_authenticated = _db_get_authenticated(browser_id)

    started = time.perf_counter()
    async with state.mcp_lock:
        answer = await client.ask(
            question=message,
            session=state.session,
            is_authenticated=is_authenticated,
            verbose=False,
        )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    ask_meta = client.get_last_ask_meta()
    tools_used = ask_meta.get("tools_used", [])
    if not tools_used:
        tools_used = ["get_current_date"]

    clean_answer, source_url = _extract_source_link(answer)
    is_supported = _infer_supported_answer(clean_answer)

    async with state.db_lock:
        assistant_message_id = _db_add_message(
            conversation_id,
            "assistant",
            clean_answer,
            source_url,
            response_ms=elapsed_ms,
            tools_used=tools_used,
            mcp_calls=ask_meta.get("mcp_calls", 0),
            is_supported=is_supported,
        )

    return {
        "conversation_id": conversation_id,
        "answer": clean_answer,
        "source_url": source_url,
        "assistant_message_id": assistant_message_id,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web_app:app", host="127.0.0.1", port=8000, reload=False)
