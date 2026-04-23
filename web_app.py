#!/usr/bin/env python3
"""
Aplicacao web (UI user-friendly) para o assistente SIGARRA.
"""

import asyncio
import sqlite3
import os
import re
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
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
DB_PATH = BASE_DIR / "sigarra_ui.db"


def _extract_source_link(answer: str) -> tuple[str, str | None]:
    """Separa a resposta do sufixo 'Fonte: <url>' e devolve link clicavel."""
    match = re.search(r"\n\nFonte:\s*(https?://\S+)\s*$", answer.strip())
    if not match:
        return answer.strip(), None

    source_url = match.group(1)
    clean_answer = answer[: match.start()].strip()
    return clean_answer, source_url


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


def _db_add_message(conversation_id: int, role: str, text: str, source_url: str | None = None) -> None:
    now = _utc_now()
    state.db.execute(
        "INSERT INTO messages(conversation_id, role, text, source_url, created_at) VALUES(?, ?, ?, ?, ?)",
        (conversation_id, role, text, source_url, now),
    )
    state.db.execute(
        "UPDATE conversations SET updated_at = ? WHERE id = ?",
        (now, conversation_id),
    )
    state.db.commit()


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
            "SELECT role, text, source_url, created_at FROM messages WHERE conversation_id = ? ORDER BY id ASC",
            (conversation_id,),
        ).fetchall()

    return {
        "messages": [
            {
                "role": row[0],
                "text": row[1],
                "source_url": row[2],
                "created_at": row[3],
            }
            for row in rows
        ]
    }


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

    async with state.mcp_lock:
        answer = await client.ask(
            question=message,
            session=state.session,
            is_authenticated=is_authenticated,
            verbose=False,
        )

    clean_answer, source_url = _extract_source_link(answer)

    async with state.db_lock:
        _db_add_message(conversation_id, "assistant", clean_answer, source_url)

    return {
        "conversation_id": conversation_id,
        "answer": clean_answer,
        "source_url": source_url,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web_app:app", host="127.0.0.1", port=8000, reload=False)
