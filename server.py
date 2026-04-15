#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Server para consulta de informações da FEUP no SIGARRA.
"""

import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------
BASE_URL = "https://sigarra.up.pt/feup/pt"

CALENDAR_URL = f"{BASE_URL}/web_base.gera_pagina?p_pagina=calend%c3%a1rio%20escolar"
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

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-PT,pt;q=0.9",
}
MAX_TEXT_CHARS = 10_000
SESSION_TIMEOUT = 2 * 60 * 60  # 2 horas em segundos

mcp = FastMCP("SIGARRA-FEUP")


# ---------------------------------------------------------------------------
# Sistema de Sessão com Cookies
# ---------------------------------------------------------------------------
@dataclass
class SigarraSession:
    authenticated: bool = False
    codigo: int | None = None
    nome: str | None = None
    tipo: str | None = None
    login: str | None = None
    error_msg: str | None = None
    cookies: dict = field(default_factory=dict)
    login_time: float = 0.0

_session = SigarraSession()


def _is_session_valid() -> bool:
    if not _session.authenticated:
        return False
    if time.time() - _session.login_time > SESSION_TIMEOUT:
        return False
    return True


# ---------------------------------------------------------------------------
# Auxiliares de Fetch
# ---------------------------------------------------------------------------
async def _fetch_json(url: str, params: dict = None) -> dict:
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        response = await client.get(url, headers=HEADERS, params=params)
        response.raise_for_status()
        return response.json()


async def _make_auth_request(url: str, params: dict = None) -> dict:
    """Faz pedidos autenticados usando os cookies da sessão."""
    if not _session.authenticated:
        raise ValueError("Não está autenticado. Use a ferramenta 'login' primeiro.")
    if not _is_session_valid():
        raise ValueError("Sessão expirada. Por favor, faça login novamente.")
    
    full_params = {'pv_codigo': str(_session.codigo)}
    if params:
        full_params.update(params)
        
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, cookies=_session.cookies) as client:
        response = await client.get(url, params=full_params, headers=HEADERS)
        response.raise_for_status()
        return response.json()


async def _fetch_and_parse() -> str:
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        response = await client.get(CALENDAR_URL, headers=HEADERS)
        response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    main = soup.find("div", id="conteudo") or soup.find("div", class_="conteudo") or soup.find("main") or soup.body

    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()

    text = main.get_text(separator="\n", strip=True)
    return "\n".join([ln for ln in text.splitlines() if ln.strip()])


def _clean_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'<li>', '\n• ', text)
    text = re.sub(r'<[^>]+>', '', text)
    return text.replace('&amp;', '&').strip()


def _normalize_search_text(text: str) -> str:
    """Remove acentos para melhorar pesquisas em endpoints sensíveis a diacríticos."""
    normalized = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def _extract_course_results(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results, seen = [], set()
    for link in soup.find_all("a", href=re.compile(r"ucurr_geral\.ficha_uc_view")):
        match = re.search(r"pv_ocorrencia_id=(\d+)", link.get("href", ""))
        nome = link.get_text(strip=True)
        if match and nome and match.group(1) not in seen:
            seen.add(match.group(1))
            results.append({"nome": nome, "id": match.group(1)})
    return results


def _course_match_score(course_name: str, query: str) -> int:
    """Atribui pontuação para priorizar o resultado mais próximo da pesquisa."""
    course_norm = _normalize_search_text(course_name).lower().strip()
    query_norm = _normalize_search_text(query).lower().strip()
    if not query_norm:
        return 0

    score = 0
    if course_norm == query_norm:
        score += 10_000
    if course_norm.startswith(query_norm):
        score += 5_000
    if query_norm in course_norm:
        score += 2_000

    query_tokens = [tok for tok in re.split(r"\W+", query_norm) if tok]
    course_tokens = set(tok for tok in re.split(r"\W+", course_norm) if tok)
    common_tokens = sum(1 for tok in query_tokens if tok in course_tokens)
    score += common_tokens * 100

    # Em empate, prefere nomes mais curtos e mais específicos.
    score -= abs(len(course_norm) - len(query_norm))
    return score


# ---------------------------------------------------------------------------
# Ferramentas MCP - Públicas
# ---------------------------------------------------------------------------
@mcp.tool()
async def get_academic_calendar() -> str:
    try:
        text = await _fetch_and_parse()
        return text[:MAX_TEXT_CHARS] + "\n… [conteúdo truncado]" if len(text) > MAX_TEXT_CHARS else text
    except Exception as exc:
        return f"Erro ao obter o calendário: {exc}"


@mcp.tool()
async def search_calendar_events(query: str) -> str:
    if not query.strip():
        return "Por favor, indique um termo de pesquisa."
    try:
        lines = (await _fetch_and_parse()).splitlines()
        query_lower = query.strip().lower()
        collected, seen = [], set()

        for i, line in enumerate(lines):
            if query_lower in line.lower():
                for j in range(max(0, i - 2), min(len(lines), i + 3)):
                    if j not in seen:
                        collected.append(lines[j])
                        seen.add(j)
                collected.append("---")

        return "\n".join(collected[:300]) if collected else f"Nenhum resultado para '{query}'."
    except Exception as exc:
        return f"Erro ao pesquisar calendário: {exc}"


@mcp.tool()
async def get_current_date() -> str:
    now = datetime.now()
    dias_pt = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
    meses_pt = ["", "janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]
    return f"{dias_pt[now.weekday()]}, {now.day} de {meses_pt[now.month]} de {now.year} ({now.strftime('%H:%M')})"


@mcp.tool()
async def search_teachers(nome: str) -> str:
    if not nome.strip():
        return "Indique um nome para pesquisar."
    try:
        data = await _fetch_json(TEACHER_SEARCH_URL, {"pv_nome": nome.strip()})
        resultados = data.get('resultados', [])
        if not resultados:
            return f"Nenhum docente encontrado com '{nome}'."
        
        lines = [f"Encontrados {data.get('total', 0)} docente(s):"]
        for r in resultados[:10]:
            lines.append(f"- {r.get('nome')} ({r.get('sigla')}) — código: {r.get('codigo')}")
        return "\n".join(lines)
    except Exception as exc:
        return f"Erro ao pesquisar docentes: {exc}"


@mcp.tool()
async def search_courses(query: str, ano_lectivo: int = 2025) -> str:
    if not query.strip():
        return "Indique um nome ou sigla."
    try:
        query_raw = query.strip()
        query_ascii = _normalize_search_text(query_raw)
        query_sigla = re.sub(r"\s+", "", query_raw).upper()
        looks_like_sigla = bool(re.fullmatch(r"[A-Z0-9]{2,10}", query_sigla))

        attempt_params = [
            {"pv_ano_lectivo": ano_lectivo, "pv_uc_nome": query_raw},
        ]
        if query_ascii.lower() != query_raw.lower():
            attempt_params.append({"pv_ano_lectivo": ano_lectivo, "pv_uc_nome": query_ascii})
        if looks_like_sigla:
            attempt_params.append({"pv_ano_lectivo": ano_lectivo, "pv_uc_sigla": query_sigla})

        results = []
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            for params in attempt_params:
                res = await client.get(COURSE_SEARCH_URL, params=params, headers=HEADERS)
                res.raise_for_status()
                results = _extract_course_results(res.text)
                if results:
                    results.sort(key=lambda r: _course_match_score(r["nome"], query_raw), reverse=True)
                    break

        if not results:
            return f"Nenhuma UC encontrada com '{query}'."
        
        lines = [f"Encontradas {len(results)} UC(s):"]
        for r in results[:15]:
            lines.append(f"- {r['nome']} — ocorrencia_id: {r['id']}")
        return "\n".join(lines)
    except Exception as exc:
        return f"Erro ao pesquisar UCs: {exc}"


@mcp.tool()
async def get_parking_status() -> str:
    try:
        data = await _fetch_json(PARKING_URL)
        info = data["itdc"][0]["resposta"]
        return (
            "Estado dos parques da FEUP:\n"
            f"P1 → {info['p1livres']} livres / {info['p1ocupados']} ocupados\n"
            f"P3 → {info['p3livres']} livres / {info['p3ocupados']} ocupados\n"
            f"P4 → {info['p4livres']} livres / {info['p4ocupados']} ocupados"
        )
    except Exception as exc:
        return f"Erro ao obter estacionamento: {exc}"


@mcp.tool()
async def get_canteen_menu() -> str:
    try:
        data = await _fetch_json(CANTEEN_URL)
        menu_text = "Ementa FEUP:\n\n"
        for place in data:
            if not place.get("ementas"): continue
            menu_text += f"**{place['descricao']}**\n"
            for day in place["ementas"]:
                menu_text += f"  {day['data']}\n"
                for dish in day["pratos"]:
                    menu_text += f"   - {dish['tipo_descr']}: {dish['descricao']}\n"
            menu_text += "\n"
        return menu_text
    except Exception as exc:
        return f"Erro ao obter ementa: {exc}"


@mcp.tool()
async def get_teacher_profile(codigo: int) -> str:
    try:
        data = await _fetch_json(TEACHER_PROFILE_URL, {"pv_codigo": codigo})
        lines = [
            f"Nome: {data.get('nome', 'N/A')}",
            f"Sigla: {data.get('sigla', 'N/A')}",
            f"Email: {data.get('email', 'N/A')}",
        ]
        if salas := data.get('salas', []):
            lines.append(f"Gabinete: {', '.join(s.get('sigla', '') for s in salas)}")
        if ext := data.get('voip_ext'):
            lines.append(f"Extensão: {ext}")
        if apr := _clean_html(data.get('apresentacao', '')):
            lines.append(f"\nApresentação:\n{apr[:500]}...")
        return "\n".join(lines)
    except Exception as exc:
        return f"Erro ao obter docente {codigo}: {exc}"


@mcp.tool()
async def get_course_info(ocorrencia_id: int) -> str:
    try:
        data = await _fetch_json(COURSE_INFO_URL, {"pv_ocorrencia_id": ocorrencia_id})
        lines = [
            f"Nome: {data.get('nome', 'N/A')} ({data.get('sigla', 'N/A')})",
            f"Ano Letivo: {data.get('ano_lectivo', 'N/A')}",
        ]
        if obj := _clean_html(data.get('objectivos', '')):
            lines.append(f"\nObjetivos:\n{obj[:300]}...")
        if aval := data.get('comp_avaliacao', []):
            lines.append("\nAvaliação:")
            for c in aval:
                if c.get('peso') and c.get('tipo_descr'):
                    lines.append(f" - {c['tipo_descr']}: {c['peso']}%")
        return "\n".join(lines)
    except Exception as exc:
        return f"Erro ao obter UC {ocorrencia_id}: {exc}"


# ---------------------------------------------------------------------------
# Ferramentas de Autenticação
# ---------------------------------------------------------------------------
@mcp.tool()
async def login(username: str, password: str) -> str:
    global _session
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            res = await client.get(LOGIN_URL, params={'pv_login': username, 'pv_password': password}, headers=HEADERS)
            res.raise_for_status()
            data = res.json()
            
            if not data.get('authenticated'):
                _session = SigarraSession(authenticated=False, error_msg="Credenciais inválidas")
                return "Autenticação falhada."

            codigo = data.get('codigo')
            nome = username
            if codigo:
                try:
                    prof_res = await client.get(STUDENT_PROFILE_URL, params={'pv_codigo': codigo}, headers=HEADERS)
                    if prof_res.status_code == 200:
                        nome = prof_res.json().get('nome', username)
                except Exception:
                    pass

            _session = SigarraSession(
                authenticated=True,
                codigo=int(codigo) if codigo else None,
                nome=nome,
                tipo=data.get('tipo', 'A'),
                login=username,
                cookies=dict(client.cookies),
                login_time=time.time(),
            )
            return f"Login bem-sucedido! Bem-vindo(a), {_session.nome}."
    except Exception as exc:
        _session = SigarraSession(authenticated=False, error_msg=str(exc))
        return "Erro ao autenticar: falha na conexão ou credenciais inválidas."


@mcp.tool()
async def logout() -> str:
    global _session
    if not _session.authenticated:
        return "Não existe sessão activa."
    nome = _session.nome
    _session = SigarraSession()
    return f"Sessão terminada. Até breve, {nome}!"


@mcp.tool()
async def get_session_status() -> str:
    if _session.authenticated:
        return f"Sessão activa: {_session.nome} ({_session.codigo})"
    return f"Nenhuma sessão activa. Último erro: {_session.error_msg}" if _session.error_msg else "Nenhuma sessão activa."


# ---------------------------------------------------------------------------
# Ferramentas MCP - Autenticadas
# ---------------------------------------------------------------------------
@mcp.tool()
async def get_my_schedule(semanas: int = 1) -> str:
    try:
        hoje = datetime.now()
        seg = hoje - timedelta(days=hoje.weekday())
        dom = seg + timedelta(days=6 + (semanas - 1) * 7)
        
        data = await _make_auth_request(SCHEDULE_URL, {
            'pv_semana_ini': seg.strftime('%Y%m%d'),
            'pv_semana_fim': dom.strftime('%Y%m%d')
        })
        
        horario = data.get('horario', [])
        if not horario:
            return "Não há aulas registadas no horário para este período."
            
        aulas_por_dia = {}
        for aula in horario:
            aulas_por_dia.setdefault(aula.get('dia', 0), []).append(aula)
            
        lines = [f"Horário ({seg.strftime('%d/%m')} - {dom.strftime('%d/%m/%Y')}):"]
        dias_pt = ['', 'Domingo', 'Segunda', 'Terça', 'Quarta', 'Quinta', 'Sexta', 'Sábado']
        
        for dia in sorted(aulas_por_dia.keys()):
            lines.append(f"\n{dias_pt[dia] if 0 < dia < len(dias_pt) else f'Dia {dia}'}:")
            for aula in sorted(aulas_por_dia[dia], key=lambda x: x.get('hora_inicio', 0)):
                h_ini, m_ini = divmod(aula.get('hora_inicio', 0) // 60, 60)
                h_fim, m_fim = divmod((aula.get('hora_inicio', 0) + aula.get('aula_duracao', 1)*3600) // 60, 60)
                lines.append(f"  {h_ini:02d}:{m_ini:02d}-{h_fim:02d}:{m_fim:02d} | {aula.get('ucurr_sigla', '?')} ({aula.get('tipo', '?')}) | Sala: {aula.get('sala_sigla', '?')}")
        return "\n".join(lines)
    except Exception as exc:
        return str(exc)


@mcp.tool()
async def get_my_exams() -> str:
    try:
        data = await _make_auth_request(EXAMS_URL)
        exames = data if isinstance(data, list) else data.get('exames', [])
        if not exames: return "Não tens exames marcados."
        
        lines = ["Exames Inscritos:"]
        for exam in exames:
            lines.append(f"• {exam.get('ucurr_sigla', '?')} - {exam.get('data', '?')} às {exam.get('hora', '?')} (Sala: {exam.get('sala', '?')})")
        return "\n".join(lines)
    except Exception as exc:
        return str(exc)


@mcp.tool()
async def get_my_profile() -> str:
    try:
        data = await _make_auth_request(MY_PROFILE_URL)
        cursos = data if isinstance(data, list) else [data]
        lines = [f"Perfil de Estudante ({_session.nome}):"]
        
        for curso in cursos:
            lines.append(f"\n📚 {curso.get('cur_nome', '?')} ({curso.get('cur_sigla', '')})")
            lines.append(f"  Média: {curso.get('media', 'N/A')}")
            insc = curso.get('inscricoes', [])
            aprovadas = [uc for uc in insc if uc.get('resultado_insc') == 'A']
            lines.append(f"  UCs aprovadas: {len(aprovadas)}/{len(insc)}")
        return "\n".join(lines)
    except Exception as exc:
        return str(exc)


@mcp.tool()
async def get_my_enrollments() -> str:
    try:
        data = await _make_auth_request(ENROLLMENTS_URL)
        cursos = data if isinstance(data, list) else [data]
        lines = ["Inscrições atuais:"]
        
        for curso in cursos:
            lines.append(f"\n📚 {curso.get('cur_nome', '?')}")
            for uc in curso.get('inscricoes', []):
                lines.append(f"  • {uc.get('ucurr_nome', '?')} ({uc.get('creditos_ects', 0)} ECTS)")
        return "\n".join(lines)
    except Exception as exc:
        return str(exc)


@mcp.tool()
async def get_my_grades() -> str:
    try:
        data = await _make_auth_request(MY_PROFILE_URL)
        lines = ["📊 Notas do Percurso Académico:\n"]
        for entry in data:
            lines.append(f"**{entry.get('cur_nome', 'Curso')}**")
            for insc in entry.get("inscricoes", []):
                if insc.get("resultado_insc") == "A":
                    lines.append(f"  • {insc.get('ucurr_sigla', '?')}: {insc.get('resultado_melhor', '?')} valores ({insc.get('per_nome', '')})")
            lines.append("")
        return "\n".join(lines)
    except Exception as exc:
        return str(exc)


if __name__ == "__main__":
    mcp.run()