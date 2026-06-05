# SIGARRA MCP

Cliente, servidor MCP (Model Context Protocol) e interface web user-friendly para consulta de dados da FEUP no SIGARRA.

## Ferramentas Disponíveis

| Ferramenta | Descrição |
|------------|-----------|
| `get_academic_calendar` | Obtém o calendário escolar completo da FEUP |
| `search_calendar_events` | Pesquisa eventos por palavra-chave |
| `get_current_date` | Devolve a data/hora atual |
| `search_teachers` + `get_teacher_profile` | Pesquisa docentes e mostra perfil |
| `search_courses` + `get_course_info` | Pesquisa UCs e mostra ficha |
| `get_parking_status` | Estado dos parques da FEUP |
| `get_canteen_menu` | Ementa das cantinas |
| `login/logout/get_session_status` | Gestão de sessão autenticada |
| `get_my_schedule`, `get_my_exams`, `get_my_profile`, `get_my_enrollments`, `get_my_grades` | Dados pessoais após login |

## Instalação

```bash
# Criar ambiente virtual
python -m venv venv
venv\Scripts\activate  # Windows
source venv/bin/activate  # Linux/Mac

# Instalar dependências
pip install -r requirements.txt
```

## Configuração

Criar ficheiro `.env` com:

```env
API_KEY=sk-usr-...
CHANNEL_ID=cmm2882wh19xwj601blybmzhy
OIDC_CLIENT_ID=...
OIDC_CLIENT_SECRET=...
OIDC_REDIRECT_URI=https://SEU_DOMINIO/login/oidc/callback
```

Se a autenticação federada não estiver configurada, a UI continua a oferecer o login SIGARRA antigo como fallback manual.

## Uso

### Modo Interactivo
```bash
python client.py
```

### Pergunta Directa
```bash
python client.py "Quando terminam as aulas do 1.º semestre?"
```

### Interface Web (UI estilo chat)
```bash
python web_app.py
```

Depois abrir no browser:

```text
http://127.0.0.1:8000
```

A UI inclui:
- autenticação federada OIDC como caminho preferencial, com fallback para login SIGARRA;
- chat em linguagem natural;
- login/logout SIGARRA;
- historico de conversas (lista na barra lateral);
- alternancia entre conversas guardadas;
- persistencia local em SQLite (`sigarra_ui.db`) no diretorio do projeto;
- identificacao por cookie HTTP-only para manter contexto do browser;
- fonte clicável para página web SIGARRA (não JSON), sempre que disponível.

### Executar Servidor MCP
```bash
python server.py
```

## Estrutura

```
├── client.py        # Cliente MCP + integração com API iaedu.pt
├── server.py        # Servidor MCP com ferramentas SIGARRA
├── web_app.py       # Backend web (FastAPI) para UI
├── web/             # Frontend (HTML/CSS/JS)
├── requirements.txt # Dependências Python
├── .env.example     # Template de configuração
└── .gitignore
```

## Requisitos

- Python 3.10+
- Acesso à API iaedu.pt (requer API_KEY)
