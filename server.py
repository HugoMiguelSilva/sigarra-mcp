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
from urllib.parse import urljoin

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
CURRENT_ACCOUNT_URL = f"{BASE_URL}/gpag_ccorrente_geral.conta_corrente_view"
PAYMENT_URL = f"{BASE_URL}/gpag_ccorrente_geral.mb"

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


async def _make_auth_request_text(url: str, params: dict = None) -> str:
    """Faz pedidos autenticados e devolve o corpo em texto (HTML)."""
    if not _session.authenticated:
        raise ValueError("Não está autenticado. Use a ferramenta 'login' primeiro.")
    if not _is_session_valid():
        raise ValueError("Sessão expirada. Por favor, faça login novamente.")

    async with httpx.AsyncClient(timeout=20, follow_redirects=True, cookies=_session.cookies) as client:
        response = await client.get(url, params=params or {}, headers=HEADERS)
        response.raise_for_status()
        return response.text


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


def _coerce_float(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace("€", "")
        cleaned = cleaned.replace(" ", "").replace(".", "").replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _find_saldo_total(html: str) -> float | None:
    """Extrai o saldo total da conta corrente (span com id 'span_saldo_total')."""
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    span = soup.find("span", id="span_saldo_total")
    if span:
        return _coerce_float(span.get_text(strip=True))
    return None


def _find_saldo_vencido(html: str) -> float | None:
    """Extrai o saldo vencido do div com class 'alerta'."""
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    alerta = soup.find("div", class_="alerta")
    if alerta:
        text = alerta.get_text(strip=True)
        match = re.search(r"Saldo\s+vencido\s*:\s*(-?\d{1,3}(?:[\.\s]\d{3})*(?:,\d{2})|\d+(?:,\d{2})?)", text, re.IGNORECASE)
        if match:
            return _coerce_float(match.group(1))
    return None


def _find_overdue_amount_in_text(text: str) -> float | None:
    """Mantida para compatibilidade - delega para _find_saldo_vencido."""
    return _find_saldo_vencido(text)


def _find_first_payment_link(html: str) -> str | None:
    """Encontra o primeiro link de pagamento na tabela de despesas não saldadas."""
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    link = soup.find("td", class_="l a")
    if link:
        a_tag = link.find("a")
        if a_tag and a_tag.get("href"):
            return urljoin(BASE_URL, a_tag["href"])
    return None


def _extract_payment_info(html: str) -> dict:
    """Extrai informações de pagamento da página de junção de débitos."""
    if not html:
        return {}
    
    soup = BeautifulSoup(html, "html.parser")
    info = {}
    
    # Extrair Valor Total
    valor_total_td = soup.find("td", string=re.compile(r"Valor Total\s*:", re.IGNORECASE))
    if valor_total_td:
        valor_total_row = valor_total_td.find_parent("tr")
        if valor_total_row:
            valor_cell = valor_total_row.find_all("td")[-1]
            if valor_cell:
                valor_text = valor_cell.get_text(strip=True)
                valor_match = re.search(r"(-?\d{1,3}(?:[\.\s]\d{3})*(?:,\d{2})|\d+(?:,\d{2})?)", valor_text)
                if valor_match:
                    info["valor_total"] = _coerce_float(valor_match.group(1))
    
    # Extrair dados da entidade
    legenda_tds = soup.find_all("td", class_="formulario-legenda")
    for td in legenda_tds:
        texto = td.get_text(strip=True)
        valor_td = td.find_next_sibling("td")
        if valor_td:
            valor = valor_td.get_text(strip=True)
            
            if "Cliente" in texto:
                info["cliente"] = valor
            elif "N.I.F." in texto:
                info["nif"] = valor
            elif "Morada" in texto:
                info["morada"] = valor
            elif "Código Postal" in texto:
                info["codigo_postal"] = valor
            elif "Localidade" in texto:
                info["localidade"] = valor
    
    # Extrair débitos associados
    debitos = []
    tabela_debitos = None
    for table in soup.find_all("table", class_="formulario"):
        if table.find("th", string=re.compile(r"Débito Associado", re.IGNORECASE)):
            tabela_debitos = table
            break
    
    if tabela_debitos:
        rows = tabela_debitos.find_all("tr")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) >= 4:
                descricao = cells[0].get_text(strip=True) if cells[0] else ""
                valor = cells[1].get_text(strip=True) if len(cells) > 1 else ""
                juro = cells[2].get_text(strip=True) if len(cells) > 2 else ""
                data_limite = cells[3].get_text(strip=True) if len(cells) > 3 else ""
                
                if descricao and "Débito Associado" not in descricao and "Valor" not in descricao:
                    debito = {
                        "descricao": descricao,
                        "valor": valor.replace("\xa0", " "),
                        "juro": juro.replace("\xa0", " ") if juro else None,
                        "data_limite": data_limite if data_limite else None
                    }
                    debitos.append(debito)
    
    if debitos:
        info["debitos"] = debitos
    
    return info


def _extract_csrf_token(html: str) -> str | None:
    """Extrai o token CSRF de uma página Django."""
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    csrf_input = soup.find("input", {"name": "csrfmiddlewaretoken"})
    if csrf_input and csrf_input.get("value"):
        return csrf_input["value"]
    return None


def _extract_mb_data(html: str) -> dict | None:
    """Extrai Entidade, Referência e Valor da página de confirmação Multibanco."""
    if not html:
        return None
    
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n", strip=True)
    
    info = {}
    
    # Padrões para dados MB
    entidade_patterns = [
        r'Entidade\s*:?\s*(\d{4,6})',
        r'Entity\s*:?\s*(\d{4,6})',
    ]
    referencia_patterns = [
        r'Referência\s*:?\s*(\d{9,15})',
        r'Reference\s*:?\s*(\d{9,15})',
    ]
    valor_patterns = [
        r'(?:Valor|Montante|Amount)\s*:?\s*(-?\d{1,3}(?:[\.\s]\d{3})*(?:,\d{2})|\d+(?:,\d{2})?)\s*(?:€|EUR)?',
    ]
    
    for pattern in entidade_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            info["entidade"] = match.group(1)
            break
    
    for pattern in referencia_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            info["referencia"] = match.group(1)
            break
    
    for pattern in valor_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            info["valor"] = match.group(1).strip()
            break
    
    # Se não encontrou, procura em elementos específicos
    if not info:
        for elem in soup.find_all(["span", "div", "td", "p", "li"]):
            elem_text = elem.get_text(strip=True)
            if "entidade" in elem_text.lower() and "entidade" not in info:
                nums = re.findall(r'\d{4,6}', elem_text)
                if nums:
                    info["entidade"] = nums[0]
            if ("referência" in elem_text.lower() or "referencia" in elem_text.lower()) and "referencia" not in info:
                nums = re.findall(r'\d{9,15}', elem_text)
                if nums:
                    info["referencia"] = nums[0]
    
    return info if info else None


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
                _session = SigarraSession(authenticated=False, error_msg="Falha na autenticação")
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
        _session = SigarraSession(authenticated=False, error_msg="Falha na autenticação")
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
    return "Nenhuma sessão activa."


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


@mcp.tool()
async def get_my_current_account() -> str:
    """Obtém informação completa da conta corrente: saldo total e saldo vencido."""
    try:
        html = await _make_auth_request_text(CURRENT_ACCOUNT_URL, {"pct_cod": str(_session.codigo)})
        
        saldo_total = _find_saldo_total(html)
        saldo_vencido = _find_saldo_vencido(html)
        
        parts = []
        if saldo_total is not None:
            parts.append(f"Saldo total: {saldo_total:.2f} EUR")
        if saldo_vencido is not None:
            parts.append(f"Saldo vencido: {saldo_vencido:.2f} EUR")
        
        if not parts:
            return "Conta corrente: não foi possível identificar os valores na resposta do SIGARRA."
        
        return "Conta corrente:\n" + "\n".join(parts)
    except Exception as exc:
        return f"Erro ao obter conta corrente: {exc}"


@mcp.tool()
async def get_payment_info() -> str:
    """Obtém informações de pagamento para o débito mais antigo em atraso."""
    if not _session.authenticated:
        return "Precisas de estar autenticado para obter informações de pagamento."
    
    try:
        html = await _make_auth_request_text(CURRENT_ACCOUNT_URL, {"pct_cod": str(_session.codigo)})
        
        payment_link = _find_first_payment_link(html)
        if not payment_link:
            return "Não foram encontrados débitos pendentes para pagamento."
        
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, cookies=_session.cookies) as client:
            response = await client.get(payment_link, headers=HEADERS)
            response.raise_for_status()
            payment_html = response.text
        
        payment_info = _extract_payment_info(payment_html)
        
        if not payment_info:
            return "Não foi possível extrair as informações de pagamento."
        
        lines = ["📋 Informações de Pagamento:"]
        
        if "cliente" in payment_info:
            lines.append(f"👤 Cliente: {payment_info['cliente']}")
        if "nif" in payment_info:
            lines.append(f"📄 NIF: {payment_info['nif']}")
        if "valor_total" in payment_info:
            lines.append(f"💰 Valor Total a Pagar: {payment_info['valor_total']:.2f} EUR")
        
        if "debitos" in payment_info:
            lines.append("\n📌 Débitos associados:")
            for debito in payment_info["debitos"]:
                linha = f"  • {debito['descricao']}: {debito['valor']}"
                if debito.get("juro") and debito["juro"].strip():
                    linha += f" (Juro: {debito['juro'].strip()})"
                if debito.get("data_limite"):
                    linha += f" - Data Limite: {debito['data_limite']}"
                lines.append(linha)
        
        lines.append(f"\n🔗 Para efetuar o pagamento, acede a: {payment_link}")
        lines.append("💡 Dica: Usa o comando 'gerar referência multibanco' para obteres os dados de pagamento.")
        
        return "\n".join(lines)
        
    except Exception as exc:
        return f"Erro ao obter informações de pagamento: {exc}"


@mcp.tool()
async def get_multibanco_reference() -> str:
    """Gera uma referência Multibanco para pagamento do débito mais antigo em atraso.
    
    Fluxo:
    1. Obtém a página da conta corrente
    2. Encontra o primeiro link de pagamento (débito mais antigo)
    3. Submete o formulário de pagamento
    4. Na página de gateway, seleciona "Referência Multibanco"
    5. Submete o formulário para gerar a referência
    6. Extrai e devolve Entidade, Referência e Valor
    """
    if not _session.authenticated:
        return "Precisas de estar autenticado para gerar uma referência Multibanco."
    
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, cookies=_session.cookies) as client:
            # 1. Obter a página da conta corrente
            cc_response = await client.get(
                CURRENT_ACCOUNT_URL,
                params={"pct_cod": str(_session.codigo)},
                headers=HEADERS
            )
            cc_response.raise_for_status()
            
            # 2. Encontrar o primeiro link de pagamento
            payment_link = _find_first_payment_link(cc_response.text)
            if not payment_link:
                return "Não foram encontrados débitos pendentes para pagamento."
            
            # 3. GET à página de pagamento do SIGARRA
            payment_response = await client.get(payment_link, headers=HEADERS)
            payment_response.raise_for_status()
            payment_html = payment_response.text
            
            # 4. Extrair informações do formulário e submeter
            soup = BeautifulSoup(payment_html, "html.parser")
            form = soup.find("form", {"id": "form_mb"})
            
            if not form:
                return "Não foi possível encontrar o formulário de pagamento."
            
            # Construir dados do formulário
            form_data = {}
            for input_tag in form.find_all("input"):
                name = input_tag.get("name")
                value = input_tag.get("value", "")
                if name:
                    form_data[name] = value
            
            # Submeter o formulário de pagamento
            form_action = form.get("action", "")
            if not form_action.startswith("http"):
                form_action = urljoin(BASE_URL, form_action) if form_action else str(payment_response.url)
            else:
                form_action = str(payment_response.url)
            
            submit_response = await client.post(
                form_action,
                data=form_data,
                headers={
                    **HEADERS,
                    "Referer": str(payment_response.url),
                    "Content-Type": "application/x-www-form-urlencoded",
                }
            )
            submit_response.raise_for_status()
            gateway_html = submit_response.text
            
            # 5. Agora estamos na página de gateway de pagamento
            csrf_token = _extract_csrf_token(gateway_html)
            
            if not csrf_token:
                # Tentar extrair dados diretamente se já estiver na página de resultado
                mb_data = _extract_mb_data(gateway_html)
                if mb_data:
                    lines = ["🏦 Referência Multibanco gerada com sucesso!"]
                    if "entidade" in mb_data:
                        lines.append(f"📋 Entidade: {mb_data['entidade']}")
                    if "referencia" in mb_data:
                        lines.append(f"🔢 Referência: {mb_data['referencia']}")
                    if "valor" in mb_data:
                        lines.append(f"💰 Valor: {mb_data['valor']} €")
                    return "\n".join(lines)
                
                return "Não foi possível obter o token CSRF da página de pagamento. Tenta novamente."
            
            # Construir o POST para gerar referência Multibanco
            base_gateway_url = str(submit_response.url)
            
            mbref_data = {
                "csrfmiddlewaretoken": csrf_token,
                "mbref": "True",
            }
            
            # Submeter o pedido de referência Multibanco
            mbref_response = await client.post(
                base_gateway_url,
                data=mbref_data,
                headers={
                    **HEADERS,
                    "Referer": base_gateway_url,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-CSRFToken": csrf_token,
                }
            )
            mbref_response.raise_for_status()
            mbref_html = mbref_response.text
            
            # 6. Extrair dados MB da resposta
            mb_data = _extract_mb_data(mbref_html)
            
            if mb_data:
                lines = ["🏦 Referência Multibanco gerada com sucesso!"]
                if "entidade" in mb_data:
                    lines.append(f"📋 Entidade: {mb_data['entidade']}")
                if "referencia" in mb_data:
                    lines.append(f"🔢 Referência: {mb_data['referencia']}")
                if "valor" in mb_data:
                    lines.append(f"💰 Valor: {mb_data['valor']} €")
                lines.append("\n⚠️ Prazo: A referência é válida até à data limite indicada na página de pagamento.")
                return "\n".join(lines)
            
            # Se não conseguiu extrair, devolve o texto visível da página
            soup_result = BeautifulSoup(mbref_html, "html.parser")
            visible_text = soup_result.get_text(separator="\n", strip=True)
            
            if len(visible_text) > 1500:
                visible_text = visible_text[:1500] + "\n[... texto truncado ...]"
            
            return f"Dados da página de resposta:\n\n{visible_text}"
            
    except Exception as exc:
        return f"Erro ao gerar referência Multibanco: {exc}"


if __name__ == "__main__":
    mcp.run()