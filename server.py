#!/usr/bin/env python3
"""
MCP Server para consulta de informações da FEUP no SIGARRA.

Ferramentas disponíveis:
  - get_academic_calendar   : calendário escolar completo
  - search_calendar_events  : filtra eventos por palavra-chave
  - get_current_date        : data de hoje (contexto temporal)
  - search_teachers         : pesquisa docentes por nome
  - get_teacher_profile     : perfil de um docente (nome, email, gabinete)
"""

import httpx
import time
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP
from datetime import datetime
from functools import wraps
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Sistema de Cache com TTL
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[Any, float]] = {}


def cached(ttl_seconds: int = 300):
    """
    Decorator para caching assíncrono com TTL.
    
    Args:
        ttl_seconds: Tempo de vida do cache em segundos.
                     Padrão: 300s (5 min).
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Criar chave única baseada na função e argumentos
            cache_key = f"{func.__name__}:{args}:{sorted(kwargs.items())}"
            
            # Verificar cache
            if cache_key in _cache:
                value, timestamp = _cache[cache_key]
                if time.time() - timestamp < ttl_seconds:
                    return value
            
            # Executar função e guardar em cache
            result = await func(*args, **kwargs)
            _cache[cache_key] = (result, time.time())
            return result
        return wrapper
    return decorator


def clear_cache():
    """Limpa todo o cache."""
    _cache.clear()


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------
BASE_URL = "https://sigarra.up.pt/feup/pt"

CALENDAR_URL = (
    f"{BASE_URL}/web_base.gera_pagina"
    "?p_pagina=calend%c3%a1rio%20escolar"
)
TEACHER_PROFILE_URL = f"{BASE_URL}/mob_func_geral.perfil"
TEACHER_SEARCH_URL = f"{BASE_URL}/mob_func_geral.pesquisa"
PARKING_URL = f"{BASE_URL}/instalacs_geral.ocupacao_parques"
CANTEEN_URL = f"{BASE_URL}/mob_eme_geral.cantinas"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-PT,pt;q=0.9",
}
MAX_TEXT_CHARS = 10_000

mcp = FastMCP("SIGARRA-FEUP")


# ---------------------------------------------------------------------------
# Auxiliares
# ---------------------------------------------------------------------------

@cached(ttl_seconds=3600)  # Cache de 1 hora para dados de docentes
async def _fetch_json(url: str, params: dict = None) -> dict:
    """
    Faz um pedido GET a um endpoint JSON do SIGARRA (com cache de 1h).
    
    Args:
        url: URL base do endpoint
        params: Parâmetros query string (ex: {"pv_codigo": 547486})
    
    Returns:
        Dicionário com a resposta JSON
    """
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        response = await client.get(url, headers=HEADERS, params=params)
        response.raise_for_status()
        return response.json()


@cached(ttl_seconds=1800)  # Cache de 30 minutos para o calendário
async def _fetch_and_parse() -> str:
    """Descarrega o HTML do SIGARRA e devolve texto limpo (com cache de 30 min)."""
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        response = await client.get(CALENDAR_URL, headers=HEADERS)
        response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    # Tentar isolar o conteúdo principal
    main = (
        soup.find("div", id="conteudo")
        or soup.find("div", class_="conteudo")
        or soup.find("main")
        or soup.body
        or soup
    )

    # Remover elementos de navegação / estilo / script
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()

    text = main.get_text(separator="\n", strip=True)

    # Compactar linhas em branco consecutivas
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Ferramentas MCP
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_academic_calendar() -> str:
    """
    Obtém o calendário escolar da FEUP directamente do SIGARRA.

    Devolve o texto completo da página de calendário, incluindo datas de
    início/fim de semestres, épocas de exames, feriados e outros eventos
    relevantes do ano lectivo.
    """
    try:
        text = await _fetch_and_parse()
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS] + "\n… [conteúdo truncado]"
        return text
    except httpx.HTTPStatusError as exc:
        return f"Erro HTTP {exc.response.status_code} ao aceder ao SIGARRA."
    except Exception as exc:  # noqa: BLE001
        return f"Erro ao obter o calendário: {exc}"


@mcp.tool()
async def search_calendar_events(query: str) -> str:
    """
    Pesquisa eventos no calendário escolar da FEUP por palavra-chave.

    Args:
        query: Termo a pesquisar (ex.: "exames", "férias", "natal",
               "páscoa", "inscrições", "aulas").

    Devolve as linhas do calendário que contêm o termo, com contexto de
    duas linhas antes e depois de cada ocorrência.
    """
    if not query or not query.strip():
        return "Por favor, indique um termo de pesquisa."

    try:
        text = await _fetch_and_parse()
    except Exception as exc:  # noqa: BLE001
        return f"Erro ao obter o calendário: {exc}"

    lines = text.splitlines()
    query_lower = query.strip().lower()
    context = 2
    collected: list[str] = []
    seen: set[int] = set()

    for i, line in enumerate(lines):
        if query_lower in line.lower():
            start = max(0, i - context)
            end = min(len(lines), i + context + 1)
            for j in range(start, end):
                if j not in seen:
                    collected.append(lines[j])
                    seen.add(j)
            collected.append("---")

    if collected:
        result = "\n".join(collected[:300])
        return result
    return f"Nenhum resultado encontrado para '{query}' no calendário escolar da FEUP."


@mcp.tool()
async def get_current_date() -> str:
    """
    Devolve a data e hora actuais do sistema.

    Útil para contextualizar perguntas como "quando são os próximos exames"
    ou "em que período lectivo estamos".
    """
    now = datetime.now()
    dias_pt = [
        "segunda-feira", "terça-feira", "quarta-feira",
        "quinta-feira", "sexta-feira", "sábado", "domingo",
    ]
    meses_pt = [
        "", "janeiro", "fevereiro", "março", "abril", "maio", "junho",
        "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
    ]
    return (
        f"{dias_pt[now.weekday()]}, {now.day} de {meses_pt[now.month]} "
        f"de {now.year} ({now.strftime('%H:%M')})"
    )


@mcp.tool()
async def search_teachers(nome: str) -> str:
    """
    Pesquisa docentes da FEUP por nome.

    Args:
        nome: Nome ou parte do nome do docente a pesquisar (ex: "Bruno Lima").

    Devolve uma lista de docentes encontrados com nome, sigla e código.
    Use o código para obter mais detalhes com get_teacher_profile().
    """
    if not nome or not nome.strip():
        return "Por favor, indique um nome para pesquisar."
    
    try:
        data = await _fetch_json(TEACHER_SEARCH_URL, {"pv_nome": nome.strip()})
        
        resultados = data.get('resultados', [])
        total = data.get('total', 0)
        
        if not resultados:
            return f"Nenhum docente encontrado com o nome '{nome}'."
        
        lines = [f"Encontrados {total} docente(s):"]
        for r in resultados[:10]:  # Limitar a 10 resultados
            lines.append(f"- {r.get('nome')} ({r.get('sigla')}) — código: {r.get('codigo')}")
        
        if total > 10:
            lines.append(f"... e mais {total - 10} resultados. Refine a pesquisa.")
        
        lines.append("\nUse get_teacher_profile(codigo) para ver o perfil completo.")
        return "\n".join(lines)
        
    except httpx.HTTPStatusError as exc:
        return f"Erro HTTP {exc.response.status_code} ao pesquisar docentes."
    except Exception as exc:
        return f"Erro ao pesquisar docentes: {exc}"

@mcp.tool()
@cached(ttl_seconds=60)
async def get_parking_status() -> str:
    """
    Obtém o estado dos parques de estacionamento da FEUP.
    """

    try:

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(PARKING_URL, headers=HEADERS)
            response.raise_for_status()

        data = response.json()

        info = data["itdc"][0]["resposta"]

        p1_livres = info["p1livres"]
        p1_ocup = info["p1ocupados"]

        p3_livres = info["p3livres"]
        p3_ocup = info["p3ocupados"]

        p4_livres = info["p4livres"]
        p4_ocup = info["p4ocupados"]

        return (
            "Estado dos parques da FEUP:\n\n"
            f"P1 → {p1_livres} livres / {p1_ocup} ocupados\n"
            f"P3 → {p3_livres} livres / {p3_ocup} ocupados\n"
            f"P4 → {p4_livres} livres / {p4_ocup} ocupados"
        )

    except Exception as exc:
        return f"Erro ao obter estacionamento: {exc}"

@mcp.tool()
@cached(ttl_seconds=3600)
async def get_canteen_menu() -> str:
    """
    Obtém a ementa da cantina FEUP.
    """

    try:

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(CANTEEN_URL, headers=HEADERS)
            response.raise_for_status()

        data = response.json()

        menu_text = "Ementa FEUP:\n\n"

        for place in data:

            name = place["descricao"]

            if not place["ementas"]:
                continue

            menu_text += f"{name}\n"

            for day in place["ementas"]:
                date = day["data"]

                menu_text += f"{date}\n"

                for dish in day["pratos"]:
                    tipo = dish["tipo_descr"]
                    desc = dish["descricao"]

                    menu_text += f" - {tipo}: {desc}\n"

                menu_text += "\n"

        return menu_text

    except Exception as exc:
        return f"Erro ao obter ementa: {exc}"
    
@mcp.tool()
async def get_teacher_profile(codigo: int) -> str:
    """
    Obtém o perfil de um docente da FEUP pelo seu código.

    Args:
        codigo: Código numérico do docente no SIGARRA (ex: 547486).
                Pode ser encontrado no URL do perfil do docente.

    Devolve informação como nome, email, gabinete, extensão VoIP,
    e apresentação/biografia do docente.
    """
    try:
        data = await _fetch_json(TEACHER_PROFILE_URL, {"pv_codigo": codigo})
        
        # Formatar resposta de forma legível
        lines = [
            f"Nome: {data.get('nome', 'N/A')}",
            f"Sigla: {data.get('sigla', 'N/A')}",
            f"Email: {data.get('email', 'N/A')}",
        ]
        
        # Gabinete(s)
        salas = data.get('salas', [])
        if salas:
            gabinetes = ", ".join(s.get('sigla', '') for s in salas)
            lines.append(f"Gabinete: {gabinetes}")
        
        # Extensão telefónica
        if data.get('voip_ext'):
            lines.append(f"Extensão VoIP: {data.get('voip_ext')}")
        
        # Apresentação (remover HTML básico)
        apresentacao = data.get('apresentacao', '')
        if apresentacao:
            # Limpar tags HTML simples
            import re
            apresentacao = re.sub(r'<br\s*/?>', '\n', apresentacao)
            apresentacao = re.sub(r'<[^>]+>', '', apresentacao)
            apresentacao = apresentacao.strip()
            if len(apresentacao) > 500:
                apresentacao = apresentacao[:500] + "..."
            lines.append(f"\nApresentação:\n{apresentacao}")
        
        return "\n".join(lines)
        
    except httpx.HTTPStatusError as exc:
        return f"Erro HTTP {exc.response.status_code}: docente com código {codigo} não encontrado."
    except Exception as exc:
        return f"Erro ao obter perfil do docente: {exc}"
    
    


# ---------------------------------------------------------------------------
# Ponto de entrada
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
