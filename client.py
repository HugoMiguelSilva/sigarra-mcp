#!/usr/bin/env python3
"""
Cliente MCP para perguntas em linguagem natural sobre o SIGARRA.
Usa MCP para obter dados do SIGARRA e a API da universidade (iaedu.pt) para gerar respostas.

Uso:
    # Modo interactivo (pede login primeiro):
    python client.py
    
    # Pergunta directa (sem login):
    python client.py "Quando terminam as aulas do 1.º semestre?"
"""

import asyncio
import json
import os
import re
import sys
import uuid
from datetime import datetime
from getpass import getpass
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

async def ask(question: str, session: ClientSession, is_authenticated: bool = False, verbose: bool = True) -> str:
    """
    Envia uma pergunta em linguagem natural.
    Usa MCP para obter dados do SIGARRA e a API da universidade para responder.

    Args:
        question: Pergunta do utilizador
        session: Sessão MCP já inicializada
        is_authenticated: Se o utilizador está autenticado no SIGARRA
        verbose: Se deve imprimir output detalhado

    Retorna a resposta final do modelo.
    """
    # Listar ferramentas MCP disponíveis
    tools_result = await session.list_tools()
    if verbose:
        names = [tool.name for tool in tools_result.tools]
        print(f"  [MCP] Ferramentas: {names}")

    # Obter data atual (sempre útil)
    date_result = await session.call_tool("get_current_date", arguments={})
    current_date = date_result.content[0].text if date_result.content else ""

    # Detectar tipo de pergunta e chamar tools apropriadas
    question_lower = question.lower()
    context_parts = [f"Data de hoje: {current_date}"]
    
    # Palavras-chave para docentes
    teacher_keywords = ["professor", "docente", "email", "gabinete", "contacto"]
    is_teacher_question = any(kw in question_lower for kw in teacher_keywords)
    
    # Palavras-chave para calendário
    calendar_keywords = ["calendário", "semestre", "férias", "feriado", "época", "inscrições"]
    is_calendar_question = any(kw in question_lower for kw in calendar_keywords)

    # Palavras-chave para cantina
    canteen_keywords = ["cantina", "menu", "ementa", "almoço", "jantar"]
    is_canteen_question = any(kw in question_lower for kw in canteen_keywords)

    # Palavras-chave para estacionamento
    parking_keywords = ["parque", "estacionamento", "parking", "lugares"]
    is_parking_question = any(kw in question_lower for kw in parking_keywords)
    
    # Palavras-chave para dados pessoais (requer autenticação)
    schedule_keywords = ["horário", "horario", "aulas hoje", "que aulas", "meu horário"]
    is_schedule_question = any(kw in question_lower for kw in schedule_keywords)
    
    exam_keywords = ["meus exames", "exames inscritos", "quando tenho exame", "minha prova"]
    is_exam_question = any(kw in question_lower for kw in exam_keywords)
    
    profile_keywords = ["meu perfil", "meus dados", "meu curso", "meu número"]
    is_profile_question = any(kw in question_lower for kw in profile_keywords)
    
    # Palavras-chave para notas (requer autenticação)
    grades_keywords = ["minhas notas", "minha nota", "nota", "notas", "grades", "classificação", "resultado", "média"]
    is_grades_question = any(kw in question_lower for kw in grades_keywords)
    
    # Palavras-chave para inscrições (requer autenticação)
    enrollments_keywords = ["minhas inscrições", "inscrições", "inscritos em", "que ucs", "uc inscritas", "disciplinas inscritas", "ects"]
    is_enrollments_question = any(kw in question_lower for kw in enrollments_keywords)
    
    # Perguntas genéricas de exames (calendário)
    general_exam_keywords = ["exame", "exames", "época de exames"]
    is_general_exam = any(kw in question_lower for kw in general_exam_keywords) and not is_exam_question
    if is_general_exam:
        is_calendar_question = True
    
    # Obter horário pessoal (requer autenticação)
    if is_schedule_question:
        if is_authenticated:
            if verbose:
                print("  [MCP] A obter horário pessoal...", end=" ", flush=True)
            result = await session.call_tool("get_my_schedule", arguments={})
            data = result.content[0].text if result.content else ""
            context_parts.append(f"\nHorário do estudante:\n{data}")
            if verbose:
                print("OK")
        else:
            context_parts.append("\n[NOTA: Dados de horário requerem login. O utilizador não está autenticado.]")

    # Obter exames inscritos (requer autenticação)
    if is_exam_question:
        if is_authenticated:
            if verbose:
                print("  [MCP] A obter exames inscritos...", end=" ", flush=True)
            result = await session.call_tool("get_my_exams", arguments={})
            data = result.content[0].text if result.content else ""
            context_parts.append(f"\nExames inscritos:\n{data}")
            if verbose:
                print("OK")
        else:
            context_parts.append("\n[NOTA: Dados de exames requerem login. O utilizador não está autenticado.]")

    # Obter perfil pessoal (requer autenticação)
    if is_profile_question:
        if is_authenticated:
            if verbose:
                print("  [MCP] A obter perfil...", end=" ", flush=True)
            result = await session.call_tool("get_my_profile", arguments={})
            data = result.content[0].text if result.content else ""
            context_parts.append(f"\nPerfil do estudante:\n{data}")
            if verbose:
                print("OK")
        else:
            context_parts.append("\n[NOTA: Dados de perfil requerem login. O utilizador não está autenticado.]")

    # Obter notas (requer autenticação)
    if is_grades_question:
        if is_authenticated:
            if verbose:
                print("  [MCP] A obter notas...", end=" ", flush=True)
            result = await session.call_tool("get_my_grades", arguments={})
            data = result.content[0].text if result.content else ""
            context_parts.append(f"\nNotas do estudante:\n{data}")
            if verbose:
                print("OK")
        else:
            context_parts.append("\n[NOTA: Dados de notas requerem login. Primeiro faça 'login' com suas credenciais SIGARRA.]")

    # Obter inscrições (requer autenticação)
    if is_enrollments_question:
        if is_authenticated:
            if verbose:
                print("  [MCP] A obter inscrições...", end=" ", flush=True)
            result = await session.call_tool("get_my_enrollments", arguments={})
            data = result.content[0].text if result.content else ""
            context_parts.append(f"\nInscrições do estudante:\n{data}")
            if verbose:
                print("OK")
        else:
            context_parts.append("\n[NOTA: Dados de inscrições requerem login. Primeiro faça 'login' com suas credenciais SIGARRA.]")

    # Obter dados de docentes se necessário
    if is_teacher_question:
        if verbose:
            print("  [MCP] Pesquisando docentes...", end=" ", flush=True)
        
        # Extrair possível nome do professor da pergunta
        name_match = re.search(r'(?:professor|docente|prof\.?)\s+([A-ZÀ-Ú][a-zà-ú]+(?:\s+[A-ZÀ-Ú][a-zà-ú]+)*)', question, re.IGNORECASE)
        
        if name_match:
            teacher_name = name_match.group(1)
            search_result = await session.call_tool("search_teachers", arguments={"nome": teacher_name})
            search_data = search_result.content[0].text if search_result.content else ""
            context_parts.append(f"\nResultados da pesquisa de docentes:\n{search_data}")
            
            # Obter perfis de todos os docentes encontrados (até 3)
            code_matches = re.findall(r'código:\s*(\d+)', search_data)
            for i, codigo in enumerate(code_matches[:3]):  # Máximo 3 perfis
                profile_result = await session.call_tool("get_teacher_profile", arguments={"codigo": int(codigo)})
                profile_data = profile_result.content[0].text if profile_result.content else ""
                context_parts.append(f"\nPerfil do docente {i+1}:\n{profile_data}")
        
        if verbose:
            print("OK")

    # Devolver o menu da cantina
    if is_canteen_question:
        if verbose:
            print("  [MCP] A obter menu da cantina...", end=" ", flush=True)

        result = await session.call_tool("get_canteen_menu", arguments={})
        data = result.content[0].text if result.content else ""

        context_parts.append(f"\nMenu das cantinas:\n{data}")

        if verbose:
            print("OK")

    # Obter o status do estacionamento
    if is_parking_question:
        if verbose:
            print("  [MCP] A obter estado dos parques...", end=" ", flush=True)

        result = await session.call_tool("get_parking_status", arguments={})
        data = result.content[0].text if result.content else ""

        context_parts.append(f"\nEstado dos parques:\n{data}")

        if verbose:
            print("OK")

    # Obter calendário se necessário
    if is_calendar_question:
        if verbose:
            print("  [MCP] A obter calendário...", end=" ", flush=True)
        
        calendar_result = await session.call_tool("get_academic_calendar", arguments={})
        calendar_data = calendar_result.content[0].text if calendar_result.content else ""
        context_parts.append(f"\nDados do calendário escolar da FEUP:\n---\n{calendar_data[:8000]}\n---")
        
        if verbose:
            print("OK")

    # Construir mensagem com contexto do SIGARRA (via MCP)
    enriched_message = f"""{chr(10).join(context_parts)}

Pergunta do utilizador: {question}

Responde com base nos dados acima. Se a informação não estiver disponível, indica isso claramente."""

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
    print("  Assistente SIGARRA — FEUP")
    print("  MCP Server + API iaedu.pt")
    print("=" * 60)


async def do_login(session: ClientSession) -> bool:
    """
    Pede credenciais e faz login no SIGARRA.
    Retorna True se login bem-sucedido.
    """
    print("\n" + "-" * 40)
    print("  LOGIN SIGARRA")
    print("-" * 40)
    print("(Digite Enter vazio para saltar login)\n")
    
    username = input("Username (ex: up123456789): ").strip()
    if not username:
        print("Login ignorado. A continuar sem autenticação...")
        return False
    
    password = getpass("Password: ")
    if not password:
        print("Password vazia. A continuar sem autenticação...")
        return False
    
    print("\nA autenticar...", end=" ", flush=True)
    result = await session.call_tool("login", arguments={
        "username": username,
        "password": password
    })
    response = result.content[0].text if result.content else "Erro desconhecido"
    print("\n")
    print(response)
    
    return "bem-sucedido" in response.lower()


async def interactive_mode() -> None:
    """Modo de conversa interactiva em loop com login."""
    _banner()
    
    # Conectar ao servidor MCP
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_SCRIPT)],
    )
    
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            
            # Pedir login
            is_authenticated = await do_login(session)
            
            if is_authenticated:
                print("\nPodes perguntar sobre: horário, exames, perfil, calendário, docentes, cantina, estacionamento...")
            else:
                print("\nPodes perguntar sobre: calendário, docentes, cantina, estacionamento...")
                print("(Para dados pessoais como horário/exames, faz login.)")
            
            print("\nEscreva a sua pergunta ou 'sair' para terminar.\n")

            while True:
                try:
                    question = input("Pergunta: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\nA terminar…")
                    break

                if not question:
                    continue
                if question.lower() in {"sair", "exit", "quit"}:
                    if is_authenticated:
                        await session.call_tool("logout", arguments={})
                        print("Sessão terminada.")
                    break

                print()
                await ask(question, session, is_authenticated)
                print()


async def single_question_mode(question: str) -> None:
    """Modo de pergunta única passada na linha de comandos (sem login)."""
    _banner()
    print(f"\nPergunta: {question}\n")
    
    # Conectar ao servidor MCP
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_SCRIPT)],
    )
    
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await ask(question, session, is_authenticated=False)
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
