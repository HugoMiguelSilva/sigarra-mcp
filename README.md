# SIGARRA MCP

Cliente e servidor MCP (Model Context Protocol) para consulta do calendário escolar da FEUP via SIGARRA.

## Ferramentas Disponíveis

| Ferramenta | Descrição |
|------------|-----------|
| `get_academic_calendar` | Obtém o calendário escolar completo da FEUP |
| `search_calendar_events` | Pesquisa eventos por palavra-chave |
| `get_current_date` | Devolve a data/hora atual |

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
```

## Uso

### Modo Interactivo
```bash
python client.py
```

### Pergunta Directa
```bash
python client.py "Quando terminam as aulas do 1.º semestre?"
```

### Executar Servidor MCP
```bash
python server.py
```

## Estrutura

```
├── client.py        # Cliente MCP + integração com API iaedu.pt
├── server.py        # Servidor MCP com ferramentas SIGARRA
├── requirements.txt # Dependências Python
├── .env.example     # Template de configuração
└── .gitignore
```

## Requisitos

- Python 3.10+
- Acesso à API iaedu.pt (requer API_KEY)
