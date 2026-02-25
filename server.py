#!/usr/bin/env python3
"""
MCP Server para consulta do calendário escolar da FEUP no SIGARRA.

Ferramentas disponíveis:
  - get_academic_calendar   : obtém o conteúdo completo do calendário
  - search_calendar_events  : filtra eventos por palavra-chave
  - get_current_date        : devolve a data de hoje (contexto temporal)
"""

import httpx
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP
from datetime import datetime

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------
CALENDAR_URL = (
    "https://sigarra.up.pt/feup/pt/web_base.gera_pagina"
    "?p_pagina=calend%c3%a1rio%20escolar"
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-PT,pt;q=0.9",
}
MAX_TEXT_CHARS = 10_000

mcp = FastMCP("SIGARRA-FEUP-Calendario")


# ---------------------------------------------------------------------------
# Auxiliares
# ---------------------------------------------------------------------------

async def _fetch_and_parse() -> str:
    """Descarrega o HTML do SIGARRA e devolve texto limpo."""
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


# ---------------------------------------------------------------------------
# Ponto de entrada
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
