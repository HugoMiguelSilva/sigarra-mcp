#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Server para consulta de informações da FEUP no SIGARRA.

Ferramentas disponíveis:
  Públicas:
    - get_academic_calendar   : calendário escolar completo
    - search_calendar_events  : filtra eventos por palavra-chave
    - get_current_date        : data de hoje (contexto temporal)
    - search_teachers         : pesquisa docentes por nome
    - get_teacher_profile     : perfil de um docente (nome, email, gabinete)
    - search_courses          : pesquisa UCs por nome ou sigla
    - get_course_info         : ficha de unidade curricular (UC)
    - get_parking_status      : ocupação dos parques de estacionamento
    - get_canteen_menu        : ementa da cantina
  
  Autenticação:
    - login                   : autenticar com credenciais SIGARRA
    - logout                  : terminar sessão
    - get_session_status      : verificar estado da sessão
  
  Autenticadas (requerem login):
    - get_my_schedule         : horário pessoal (semana atual)
    - get_my_exams            : exames inscritos
    - get_my_profile          : perfil e percurso académico
    - get_my_enrollments      : inscrições atuais (UCs)
    - get_my_grades           : notas detalhadas por UC
"""

from dataclasses import dataclass, field

import httpx
import time
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP
from datetime import datetime


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
COURSE_INFO_URL = f"{BASE_URL}/mob_ucurr_geral.perfil"
COURSE_SEARCH_URL = f"{BASE_URL}/ucurr_geral.pesquisa_ocorr_ucs_list"
PARKING_URL = f"{BASE_URL}/instalacs_geral.ocupacao_parques"
CANTEEN_URL = f"{BASE_URL}/mob_eme_geral.cantinas"

# URLs de autenticação e endpoints autenticados (API móvel JSON)
LOGIN_URL = f"{BASE_URL}/mob_val_geral.autentica"
SCHEDULE_URL = f"{BASE_URL}/mob_hor_geral.estudante"
EXAMS_URL = f"{BASE_URL}/mob_fest_geral.exames"
STUDENT_PROFILE_URL = f"{BASE_URL}/mob_fest_geral.perfil"
MY_PROFILE_URL = f"{BASE_URL}/mob_fest_geral.percurso_academico"
ENROLLMENTS_URL = f"{BASE_URL}/mob_fest_geral.ucurr_inscricoes_corrente"

# ---------------------------------------------------------------------------
# Sistema de Sessão com Cookies
# ---------------------------------------------------------------------------

@dataclass
class SigarraSession:
    """Guarda o estado da sessão autenticada."""
    authenticated: bool = False
    codigo: int | None = None
    nome: str | None = None
    tipo: str | None = None  # 'A' aluno, 'F' funcionário
    login: str | None = None
    error_msg: str | None = None
    cookies: dict = field(default_factory=dict)  # Cookies da sessão
    login_time: float = 0.0  # Timestamp do login para timeout

# Sessão global (apenas uma sessão activa de cada vez)
_session = SigarraSession()

# Timeout de sessão: 2 horas (em segundos)
SESSION_TIMEOUT = 2 * 60 * 60


def _is_session_valid() -> bool:
    """Verifica se a sessão ainda é válida (não expirou)."""
    if not _session.authenticated:
        return False
    if time.time() - _session.login_time > SESSION_TIMEOUT:
        return False
    return True

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

async def _fetch_authenticated_json(url: str, params: dict = None) -> dict:
    """
    Faz um pedido GET autenticado a um endpoint JSON do SIGARRA.
    
    Adiciona automaticamente os parâmetros de autenticação se a sessão estiver activa.
    
    Args:
        url: URL base do endpoint
        params: Parâmetros query string adicionais
    
    Returns:
        Dicionário com a resposta JSON
    
    Raises:
        ValueError: Se não houver sessão autenticada
    """
    if not _session.authenticated or not _session.codigo:
        raise ValueError("Sessão não autenticada. Use a ferramenta 'login' primeiro.")
    if not _is_session_valid():
        raise ValueError("Sessão expirada. Faça login novamente.")
    
    # Combinar parâmetros base com os fornecidos
    full_params = {"pv_codigo": _session.codigo}
    if params:
        full_params.update(params)
    
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        response = await client.get(url, headers=HEADERS, params=full_params)
        response.raise_for_status()
        return response.json()


async def _fetch_json(url: str, params: dict = None) -> dict:
    """
    Faz um pedido GET a um endpoint JSON do SIGARRA.
    
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
async def search_courses(query: str, ano_lectivo: int = 2025) -> str:
    """
    Pesquisa unidades curriculares (UCs) da FEUP por nome ou sigla.

    Args:
        query: Nome ou sigla da UC a pesquisar (ex: "Física", "ES", "LBAW").
        ano_lectivo: Ano letivo para pesquisa (ex: 2025). Por omissão: 2025.

    Devolve uma lista de UCs encontradas com nome e ocorrencia_id.
    Use o ocorrencia_id para obter mais detalhes com get_course_info().
    """
    import re
    
    if not query or not query.strip():
        return "Por favor, indique um nome ou sigla para pesquisar."
    
    try:
        # Pesquisar UCs usando o endpoint oficial do SIGARRA
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.get(
                COURSE_SEARCH_URL,
                params={
                    "pv_ano_lectivo": ano_lectivo,
                    "pv_uc_nome": query.strip(),
                },
                headers=HEADERS
            )
            response.raise_for_status()
        
        soup = BeautifulSoup(response.text, "html.parser")
        
        # Encontrar links de UCs na tabela de resultados
        results = []
        
        for link in soup.find_all("a", href=re.compile(r"ucurr_geral\.ficha_uc_view")):
            href = link.get("href", "")
            nome = link.get_text(strip=True)
            
            # Extrair ocorrencia_id do href
            match = re.search(r"pv_ocorrencia_id=(\d+)", href)
            if match and nome:  # Só adicionar se tiver nome e ID
                results.append({"nome": nome, "ocorrencia_id": match.group(1)})
        
        if not results:
            return f"Nenhuma UC encontrada com '{query}' no ano letivo {ano_lectivo}."
        
        # Remover duplicados (mesmo nome)
        seen = set()
        unique_results = []
        for r in results:
            if r["nome"] not in seen:
                seen.add(r["nome"])
                unique_results.append(r)
        
        lines = [f"Encontradas {len(unique_results)} UC(s) para '{query}':"]
        for r in unique_results[:15]:  # Limitar a 15 resultados
            lines.append(f"- {r['nome']} — ocorrencia_id: {r['ocorrencia_id']}")
        
        if len(unique_results) > 15:
            lines.append(f"... e mais {len(unique_results) - 15} resultados.")
        
        lines.append("\nUse get_course_info(ocorrencia_id) para ver a ficha completa.")
        return "\n".join(lines)
        
    except httpx.HTTPStatusError as exc:
        return f"Erro HTTP {exc.response.status_code} ao pesquisar UCs."
    except Exception as exc:
        return f"Erro ao pesquisar UCs: {exc}"


@mcp.tool()
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


@mcp.tool()
async def get_course_info(ocorrencia_id: int) -> str:
    """
    Obtém a ficha de uma unidade curricular (UC) da FEUP.

    Args:
        ocorrencia_id: ID da ocorrência da UC no SIGARRA (ex: 484425 para ES).
                       Encontra-se no URL da página da UC.

    Devolve informação como nome, sigla, regentes, docentes, objetivos,
    conteúdo programático, bibliografia e componentes de avaliação.
    """
    import re
    
    def clean_html(text: str) -> str:
        """Remove tags HTML e limpa texto."""
        if not text:
            return ""
        text = re.sub(r'<br\s*/?>', '\n', text)
        text = re.sub(r'<li>', '\n• ', text)
        text = re.sub(r'<[^>]+>', '', text)
        text = text.replace('&amp;', '&')
        return text.strip()
    
    try:
        data = await _fetch_json(COURSE_INFO_URL, {"pv_ocorrencia_id": ocorrencia_id})
        
        lines = [
            f"Código: {data.get('codigo', 'N/A')}",
            f"Nome: {data.get('nome', 'N/A')}",
            f"Sigla: {data.get('sigla', 'N/A')}",
            f"Ano Letivo: {data.get('ano_lectivo', 'N/A')}",
        ]
        
        # Carga horária
        carga = data.get('carga_horaria', [])
        if carga:
            horas = ", ".join(f"{c['descricao']}: {c['horas']}h" for c in carga)
            lines.append(f"Carga Horária: {horas}")
        
        # Regentes
        resp = data.get('responsabilidades', [])
        if resp:
            regentes = [f"{r['nome']} ({r['papel']})" for r in resp]
            lines.append(f"\nRegentes: {', '.join(regentes)}")
        
        # Docentes por tipo de aula
        ds = data.get('ds', [])
        if ds:
            lines.append("\nDocentes:")
            for tipo in ds:
                docentes = [d['nome'] for d in tipo.get('docentes', [])]
                if docentes:
                    lines.append(f"  {tipo['tipo_descricao']}: {', '.join(docentes[:5])}")
                    if len(docentes) > 5:
                        lines.append(f"    ... e mais {len(docentes) - 5}")
        
        # Objetivos
        obj = clean_html(data.get('objectivos', ''))
        if obj:
            if len(obj) > 300:
                obj = obj[:300] + "..."
            lines.append(f"\nObjetivos:\n{obj}")
        
        # Conteúdo programático
        conteudo = clean_html(data.get('conteudo', ''))
        if conteudo:
            if len(conteudo) > 500:
                conteudo = conteudo[:500] + "..."
            lines.append(f"\nConteúdo Programático:\n{conteudo}")
        
        # Componentes de avaliação
        aval = data.get('comp_avaliacao', [])
        if aval:
            lines.append("\nAvaliação:")
            for comp in aval:
                peso = comp.get('peso')
                tipo = comp.get('tipo_descr', '')
                if peso and tipo:  # Só mostrar se tiver peso e tipo
                    lines.append(f"  - {tipo}: {peso}%")
        
        # Bibliografia principal
        bib = data.get('bibliografia', [])
        bib_principal = [b for b in bib if b.get('tipo') == 'P']
        if bib_principal:
            lines.append("\nBibliografia Principal:")
            for b in bib_principal[:3]:
                lines.append(f"  - {b.get('autores', 'N/A')}, \"{b.get('titulo', 'N/A')}\" ({b.get('ano', 'N/A')})")
        
        return "\n".join(lines)
        
    except httpx.HTTPStatusError as exc:
        return f"Erro HTTP {exc.response.status_code}: UC com ID {ocorrencia_id} não encontrada."
    except Exception as exc:
        return f"Erro ao obter ficha da UC: {exc}"


# ---------------------------------------------------------------------------
# Ferramentas de Autenticação
# ---------------------------------------------------------------------------

@mcp.tool()
async def login(username: str, password: str) -> str:
    """
    Autentica no SIGARRA com as credenciais fornecidas.

    Args:
        username: Nome de utilizador (ex: 'up123456789' ou '123456789')
        password: Palavra-passe do SIGARRA

    Devolve informação sobre o resultado da autenticação.
    Após login bem-sucedido, as ferramentas autenticadas ficam disponíveis.
    """
    global _session
    
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            # Login via API móvel JSON
            params = {
                'pv_login': username,
                'pv_password': password
            }
            response = await client.get(LOGIN_URL, params=params, headers=HEADERS)
            response.raise_for_status()
            
            data = response.json()
            
            if data.get('authenticated'):
                codigo = data.get('codigo')
                tipo = data.get('tipo', 'A')  # 'A' aluno, 'F' funcionário
                
                # Obter nome do perfil
                nome = None
                if codigo:
                    try:
                        profile_response = await client.get(
                            STUDENT_PROFILE_URL,
                            params={'pv_codigo': codigo},
                            headers=HEADERS
                        )
                        if profile_response.status_code == 200:
                            profile_data = profile_response.json()
                            nome = profile_data.get('nome')
                    except:
                        pass
                
                _session = SigarraSession(
                    authenticated=True,
                    codigo=int(codigo) if codigo else None,
                    nome=nome or username,
                    tipo=tipo,
                    login=username,
                    cookies=dict(client.cookies),
                    login_time=time.time(),
                )
                
                tipo_str = 'Aluno' if tipo == 'A' else 'Funcionário' if tipo == 'F' else tipo
                return (
                    f"Login bem-sucedido!\n"
                    f"Nome: {_session.nome}\n"
                    f"Código: {_session.codigo}\n"
                    f"Tipo: {tipo_str}\n\n"
                    f"Agora pode usar as ferramentas autenticadas como get_my_schedule e get_my_exams."
                )
            else:
                _session = SigarraSession(
                    authenticated=False,
                    error_msg="Credenciais inválidas"
                )
                return "Falha na autenticação: credenciais inválidas."
            
    except Exception as exc:
        _session = SigarraSession(authenticated=False, error_msg=str(exc))
        return f"Erro ao autenticar: {exc}"


@mcp.tool()
async def logout() -> str:
    """
    Termina a sessão autenticada no SIGARRA.
    
    Após logout, as ferramentas autenticadas deixam de funcionar.
    """
    global _session
    
    if not _session.authenticated:
        return "Não existe sessão activa."
    
    nome = _session.nome
    _session = SigarraSession()
    return f"Sessão terminada. Até breve, {nome}!"


@mcp.tool()
async def get_session_status() -> str:
    """
    Verifica o estado actual da sessão no SIGARRA.
    
    Devolve informação sobre se o utilizador está autenticado e,
    em caso afirmativo, os detalhes da sessão.
    """
    if _session.authenticated:
        return (
            f"Sessão activa:\n"
            f"Nome: {_session.nome}\n"
            f"Código: {_session.codigo}\n"
            f"Tipo: {'Aluno' if _session.tipo == 'A' else 'Funcionário' if _session.tipo == 'F' else _session.tipo}\n"
            f"Login: {_session.login}"
        )
    else:
        msg = "Nenhuma sessão activa."
        if _session.error_msg:
            msg += f"\nÚltimo erro: {_session.error_msg}"
        return msg


@mcp.tool()
async def get_my_schedule(semanas: int = 1) -> str:
    """
    Obtém o horário pessoal do estudante autenticado.

    Args:
        semanas: Número de semanas a mostrar (1-4). Por omissão: 1 (semana atual).

    REQUER LOGIN: Use a ferramenta 'login' primeiro.
    Devolve o horário semanal com aulas, salas, docentes e horários.
    """
    from datetime import timedelta
    
    if not _session.authenticated:
        return "Erro: Não está autenticado. Use a ferramenta 'login' primeiro."
    
    if not _is_session_valid():
        return "Erro: Sessão expirada. Por favor faça login novamente."
    
    try:
        # Calcular intervalo de datas (segunda a domingo)
        hoje = datetime.now()
        seg = hoje - timedelta(days=hoje.weekday())  # Segunda desta semana
        dom = seg + timedelta(days=6 + (semanas - 1) * 7)  # Domingo da última semana
        
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, cookies=_session.cookies) as client:
            params = {
                'pv_codigo': str(_session.codigo),
                'pv_semana_ini': seg.strftime('%Y%m%d'),
                'pv_semana_fim': dom.strftime('%Y%m%d')
            }
            response = await client.get(SCHEDULE_URL, params=params, headers=HEADERS)
            response.raise_for_status()
        
        data = response.json()
        horario = data.get('horario', [])
        
        if not horario:
            return f"Não há aulas registadas no horário para as próximas {semanas} semana(s).\nIsto pode significar que ainda não estás inscrito em turmas ou estás em período de férias."
        
        # Mapear dia da semana
        dias_pt = ['', 'Domingo', 'Segunda', 'Terça', 'Quarta', 'Quinta', 'Sexta', 'Sábado']
        
        # Agrupar por dia
        aulas_por_dia = {}
        for aula in horario:
            dia = aula.get('dia', 0)
            if dia not in aulas_por_dia:
                aulas_por_dia[dia] = []
            aulas_por_dia[dia].append(aula)
        
        # Ordenar aulas por hora dentro de cada dia
        for dia in aulas_por_dia:
            aulas_por_dia[dia].sort(key=lambda x: x.get('hora_inicio', 0))
        
        # Formatar saída
        lines = [f"Horário ({seg.strftime('%d/%m')} - {dom.strftime('%d/%m/%Y')}):"]
        lines.append("=" * 50)
        
        for dia in sorted(aulas_por_dia.keys()):
            dia_nome = dias_pt[dia] if 0 < dia < len(dias_pt) else f"Dia {dia}"
            lines.append(f"\n{dia_nome}:")
            lines.append("-" * 30)
            
            for aula in aulas_por_dia[dia]:
                # Converter segundos para hora:minuto
                hora_ini = aula.get('hora_inicio', 0)
                h_ini = hora_ini // 3600
                m_ini = (hora_ini % 3600) // 60
                duracao = aula.get('aula_duracao', 1)
                hora_fim_sec = hora_ini + duracao * 3600
                h_fim = hora_fim_sec // 3600
                m_fim = (hora_fim_sec % 3600) // 60
                
                uc = aula.get('ucurr_sigla', '?')
                tipo = aula.get('tipo', '?')
                sala = aula.get('sala_sigla', '?')
                turma = aula.get('turma_sigla', '')
                
                docentes = aula.get('docentes', [])
                doc_nomes = ', '.join(d.get('doc_nome', '?') for d in docentes[:2])
                if len(docentes) > 2:
                    doc_nomes += f' (+{len(docentes)-2})'
                
                lines.append(f"  {h_ini:02d}:{m_ini:02d}-{h_fim:02d}:{m_fim:02d}  {uc} ({tipo})")
                lines.append(f"    Sala: {sala} | Turma: {turma}")
                if doc_nomes:
                    lines.append(f"    Docente: {doc_nomes}")
        
        return "\n".join(lines)
        
    except httpx.HTTPStatusError as exc:
        return f"Erro HTTP {exc.response.status_code} ao obter horário."
    except Exception as exc:
        return f"Erro ao obter horário: {exc}"


@mcp.tool()
async def get_my_exams() -> str:
    """
    Obtém a lista de exames do estudante autenticado.

    REQUER LOGIN: Use a ferramenta 'login' primeiro.
    Devolve lista de exames com datas, horas e salas.
    """
    if not _session.authenticated:
        return "Erro: Não está autenticado. Use a ferramenta 'login' primeiro."
    
    if not _is_session_valid():
        return "Erro: Sessão expirada. Por favor faça login novamente."
    
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, cookies=_session.cookies) as client:
            params = {'pv_codigo': str(_session.codigo)}
            response = await client.get(EXAMS_URL, params=params, headers=HEADERS)
            response.raise_for_status()
        
        data = response.json()
        exames = data if isinstance(data, list) else data.get('exames', [])
        
        if not exames:
            return "Não foram encontrados exames.\nVerifique se está inscrito em exames no SIGARRA."
        
        lines = ["Exames:"]
        lines.append("=" * 50)
        
        for exam in exames:
            uc = exam.get('ucurr_nome', exam.get('ucurr_sigla', '?'))
            data_str = exam.get('data', '?')
            hora = exam.get('hora', '?')
            sala = exam.get('sala', exam.get('salas', '?'))
            tipo = exam.get('tipo', exam.get('epoca', ''))
            
            lines.append(f"\n• {uc}")
            lines.append(f"  Data: {data_str} às {hora}")
            if sala:
                lines.append(f"  Sala: {sala}")
            if tipo:
                lines.append(f"  Época: {tipo}")
        
        return "\n".join(lines)
        
    except httpx.HTTPStatusError as exc:
        return f"Erro HTTP {exc.response.status_code} ao obter exames."
    except Exception as exc:
        return f"Erro ao obter exames: {exc}"


@mcp.tool()
async def get_my_profile() -> str:
    """
    Obtém o perfil e percurso académico do utilizador autenticado.

    REQUER LOGIN: Use a ferramenta 'login' primeiro.
    Devolve informação como nome, número, curso, média e UCs aprovadas.
    """
    if not _session.authenticated:
        return "Erro: Não está autenticado. Use a ferramenta 'login' primeiro."
    
    if not _is_session_valid():
        return "Erro: Sessão expirada. Por favor faça login novamente."
    
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, cookies=_session.cookies) as client:
            # Obter percurso académico
            params = {'pv_codigo': str(_session.codigo)}
            response = await client.get(MY_PROFILE_URL, params=params, headers=HEADERS)
            response.raise_for_status()
            
            data = response.json()
        
        # A resposta é uma lista de cursos
        cursos = data if isinstance(data, list) else [data]
        
        lines = [
            f"Nome: {_session.nome}",
            f"Número: {_session.codigo}",
            f"Login: {_session.login}",
        ]
        
        for curso in cursos:
            nome_curso = curso.get('cur_nome', '?')
            sigla = curso.get('cur_sigla', '')
            ano = curso.get('ano_curricular', '')
            media = curso.get('media')
            tipo = curso.get('fest_tipo_descr', '')
            
            lines.append(f"\n📚 {nome_curso} ({sigla})")
            if ano:
                lines.append(f"  Ano curricular: {ano}º")
            if tipo:
                lines.append(f"  Tipo inscrição: {tipo}")
            if media:
                lines.append(f"  Média: {media}")
            
            # Contar UCs aprovadas
            inscricoes = curso.get('inscricoes', [])
            aprovadas = [uc for uc in inscricoes if uc.get('resultado_insc') == 'A']
            if inscricoes:
                lines.append(f"  UCs aprovadas: {len(aprovadas)} de {len(inscricoes)}")
        
        return "\n".join(lines)
        
    except httpx.HTTPStatusError as exc:
        return f"Erro HTTP {exc.response.status_code} ao obter perfil."
    except Exception as exc:
        return f"Erro ao obter perfil: {exc}"


@mcp.tool()
async def get_my_enrollments() -> str:
    """
    Obtém as inscrições atuais do estudante autenticado.

    REQUER LOGIN: Use a ferramenta 'login' primeiro.
    Devolve lista de UCs em que está inscrito no período corrente.
    """
    if not _session.authenticated:
        return "Erro: Não está autenticado. Use a ferramenta 'login' primeiro."
    
    if not _is_session_valid():
        return "Erro: Sessão expirada. Por favor faça login novamente."
    
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, cookies=_session.cookies) as client:
            params = {'pv_codigo': str(_session.codigo)}
            response = await client.get(ENROLLMENTS_URL, params=params, headers=HEADERS)
            response.raise_for_status()
            
            data = response.json()
        
        # A resposta é uma lista de cursos, cada um com 'inscricoes'
        cursos = data if isinstance(data, list) else [data]
        
        lines = ["Inscrições atuais:"]
        lines.append("=" * 50)
        
        total_ects = 0
        for curso in cursos:
            nome_curso = curso.get('cur_nome', curso.get('cur_sigla', ''))
            inscricoes = curso.get('inscricoes', [])
            
            if nome_curso:
                lines.append(f"\n📚 {nome_curso}")
            
            for uc in inscricoes:
                nome = uc.get('ucurr_nome', '?')
                sigla = uc.get('ucurr_sigla', '')
                ects = uc.get('creditos_ects', 0)
                ano = uc.get('ano', '')
                periodo = uc.get('per_nome', '')
                
                lines.append(f"\n  • {nome} ({sigla})")
                if ano:
                    lines.append(f"    Ano: {ano}º")
                if periodo:
                    lines.append(f"    Período: {periodo}")
                if ects:
                    lines.append(f"    ECTS: {ects}")
                    total_ects += ects
        
        if total_ects:
            lines.append(f"\nTotal ECTS este período: {total_ects}")
        
        if len(lines) <= 2:
            return "Não foram encontradas inscrições no período corrente."
        
        return "\n".join(lines)
        
    except httpx.HTTPStatusError as exc:
        return f"Erro HTTP {exc.response.status_code} ao obter inscrições."
    except Exception as exc:
        return f"Erro ao obter inscrições: {exc}"


@mcp.tool()
async def get_my_grades() -> str:
    """
    Obtém as notas do estudante autenticado no percurso académico.
    Mostra UC, nota final, nota ECTS e semestre para cada disciplina concluída.
    Requer login prévio com authenticate_student.
    """
    if not _session.authenticated:
        return "Erro: Não está autenticado. Use a ferramenta 'login' primeiro."
    
    if not _is_session_valid():
        return "Erro: Sessão expirada. Por favor faça login novamente."
    
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, cookies=_session.cookies) as client:
            params = {'pv_codigo': str(_session.codigo)}
            response = await client.get(MY_PROFILE_URL, params=params, headers=HEADERS)
            response.raise_for_status()
            
            data = response.json()
        
        lines = ["📊 **Notas do Percurso Académico**", ""]
        
        # Percorrer anos/cursos
        for entry in data:
            curso = entry.get("cur_nome", "Curso desconhecido")
            ano_lect = entry.get("ano_lectivo", "")
            lines.append(f"**{curso}** ({ano_lect})")
            lines.append("-" * 40)
            
            inscricoes = entry.get("inscricoes", [])
            aprovadas = []
            
            for insc in inscricoes:
                resultado = insc.get("resultado_insc", "")
                if resultado == "A":  # Aprovado
                    sigla = insc.get("ucurr_sigla", "?")
                    nome = insc.get("ucurr_nome", sigla)
                    nota = insc.get("resultado_melhor", "?")
                    nota_ects = insc.get("resultado_ects", "")
                    semestre = insc.get("per_nome", "")
                    ects = insc.get("creditos_ects", "")
                    
                    info = f"  • **{sigla}**: {nota}"
                    if nota_ects:
                        info += f" (ECTS: {nota_ects})"
                    if ects:
                        info += f" [{ects} ECTS]"
                    if semestre:
                        info += f" - {semestre}"
                    aprovadas.append(info)
            
            if aprovadas:
                for a in aprovadas:
                    lines.append(a)
            else:
                lines.append("  Sem disciplinas aprovadas neste período.")
            
            lines.append("")
        
        if len(lines) <= 2:
            return "Não foram encontradas notas no percurso académico."
        
        
        return "\n".join(lines)
        
    except httpx.HTTPStatusError as exc:
        return f"Erro HTTP {exc.response.status_code} ao obter notas."
    except Exception as exc:
        return f"Erro ao obter notas: {exc}"


# ---------------------------------------------------------------------------
# Ponto de entrada
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
