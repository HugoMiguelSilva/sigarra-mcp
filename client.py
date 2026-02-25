#!/usr/bin/env python3
"""
Cliente MCP para perguntas em linguagem natural sobre o SIGARRA.
Usa MCP para obter dados do SIGARRA e a API da universidade (iaedu.pt) para gerar respostas.

Uso:
    # Pergunta directa na linha de comandos:
    python client.py "Quando terminam as aulas do 1.º semestre?"

    # Modo interactivo:
    python client.py
"""

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

load_dotenv()

API_ENDPOINT = os.getenv(
    "API_ENDPOINT",
    "https://api.iaedu.pt/agent-chat/api/v1/agent/cmamvd3n40000c801qeacoad2/stream"
)
API_KEY = os.getenv("API_KEY", "")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")
SERVER_SCRIPT = Path(__file__).parent / "server.py"


# ---------------------------------------------------------------------------
# Lógica principal
# ---------------------------------------------------------------------------

async def ask(question: str, verbose: bool = True) -> str:
    """
    Envia uma pergunta em linguagem natural.
    Usa MCP para obter dados do SIGARRA e a API da universidade para responder.

    Retorna a resposta final do modelo.
    """
    # Conectar ao servidor MCP para obter dados do SIGARRA
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_SCRIPT)],
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Listar ferramentas MCP disponíveis
            tools_result = await session.list_tools()
            if verbose:
                names = [tool.name for tool in tools_result.tools]
                print(f"  [MCP] Ferramentas: {names}")

            # Chamar ferramentas MCP para obter contexto
            if verbose:
                print("  [MCP] A obter calendário...", end=" ", flush=True)
            
            calendar_result = await session.call_tool("get_academic_calendar", arguments={})
            calendar_data = calendar_result.content[0].text if calendar_result.content else ""
            
            date_result = await session.call_tool("get_current_date", arguments={})
            current_date = date_result.content[0].text if date_result.content else ""
            
            if verbose:
                print("OK")

    # Construir mensagem com contexto do SIGARRA (via MCP)
    enriched_message = f"""Data de hoje: {current_date}

Dados do calendário escolar da FEUP (obtidos via MCP do SIGARRA):
---
{calendar_data[:8000]}
---

Pergunta do utilizador: {question}

Responde com base nos dados do calendário acima. Se a informação não estiver disponível, indica isso claramente."""

    # Enviar para a API da universidade
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "x-api-key": API_KEY,
    }
    
    thread_id = str(uuid.uuid4())
    
    payload = {
        "message": enriched_message,
        "thread_id": thread_id,
        "channel_id": CHANNEL_ID,
        "user_info": json.dumps({"id": "user", "name": "Utilizador"})
    }
    
    if verbose:
        print("Resposta: ", end="", flush=True)
    
    async with httpx.AsyncClient(timeout=120) as client:
        try:
            async with client.stream(
                "POST",
                API_ENDPOINT,
                headers=headers,
                data=payload,
            ) as response:
                if response.status_code != 200:
                    await response.aread()
                    return f"Erro HTTP {response.status_code}: {response.text}"
                
                full_response = ""
                async for chunk in response.aiter_text():
                    for line in chunk.split("\n"):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            msg_type = data.get("type", "")
                            content = data.get("content", "")
                            
                            if msg_type == "token" and isinstance(content, str):
                                full_response += content
                                if verbose:
                                    print(content, end="", flush=True)
                            elif msg_type == "message" and isinstance(content, dict):
                                final_content = content.get("content", "")
                                if final_content and not full_response:
                                    full_response = final_content
                                    
                        except json.JSONDecodeError:
                            continue
                
                if verbose:
                    print()
                    
                if full_response:
                    return full_response.strip()
                    
        except httpx.HTTPStatusError as exc:
            return f"Erro HTTP {exc.response.status_code}"
        except Exception as exc:
            return f"Erro ao comunicar com a API: {exc}"
    
    return "Não foi possível obter uma resposta."


def _banner() -> None:
    print("=" * 60)
    print("  Assistente SIGARRA — Calendário Escolar da FEUP")
    print("  MCP Server + API iaedu.pt")
    print("=" * 60)


async def interactive_mode() -> None:
    """Modo de conversa interactiva em loop."""
    _banner()
    print("Escreva a sua pergunta ou 'sair' para terminar.\n")

    while True:
        try:
            question = input("Pergunta: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nA terminar…")
            break

        if not question:
            continue
        if question.lower() in {"sair", "exit", "quit"}:
            break

        print()
        await ask(question)
        print()


async def single_question_mode(question: str) -> None:
    """Modo de pergunta única passada na linha de comandos."""
    _banner()
    print(f"\nPergunta: {question}\n")
    await ask(question)
    print()


# ---------------------------------------------------------------------------
# Ponto de entrada
# ---------------------------------------------------------------------------

def main() -> None:
    if not API_KEY:
        print(
            "Erro: a variável API_KEY não está definida.\n"
            "Crie um ficheiro .env com:\n"
            "  API_KEY=sk-usr-...\n"
            "  CHANNEL_ID=cmm2882wh19xwj601blybmzhy"
        )
        sys.exit(1)

    if len(sys.argv) > 1:
        question = " ".join(sys.argv[1:])
        asyncio.run(single_question_mode(question))
    else:
        asyncio.run(interactive_mode())


if __name__ == "__main__":
    main()
