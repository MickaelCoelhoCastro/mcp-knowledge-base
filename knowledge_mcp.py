# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""crivo-code-knowledge: servidor MCP com ZERO dependencias externas.

Fala JSON-RPC 2.0 direto no stdio, sem o SDK `mcp` -- que arrasta 14 pacotes,
dois deles com binario nativo travado por versao de Python e plataforma
(_pydantic_core.cp3XX-<os>.pyd e pywintypes3XX.dll). Sem SDK nao existe
`pip install` no lado do desenvolvedor, e o requirements.txt fica vazio.

Este arquivo e so o adaptador de protocolo: handshake, catalogo de tools e
despacho. Toda a logica esta em ../tools (code_query.py e indexer.py), que sao
tools independentes de MCP e podem ser chamadas por qualquer outro agente.

O cabecalho PEP 723 acima permite `uv run --script knowledge_mcp.py`: o uv
provisiona um Python >= 3.10 em cache se a maquina nao tiver nenhum, entao o
dev nao precisa de Python instalado. `dependencies = []` e intencional.
"""

import json
import os
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
# Instalado como pacote (uvx), as tools ficam ao lado: crivo_mcp/tools.
# Rodando do repo, ficam em .aura/tools, um nivel acima de .aura/mcp.
TOOLS_DIR = next((d for d in (os.path.join(_HERE, "tools"),
                              os.path.join(os.path.dirname(_HERE), "tools"))
                  if os.path.isfile(os.path.join(d, "code_query.py"))),
                 os.path.join(os.path.dirname(_HERE), "tools"))
sys.path.insert(0, TOOLS_DIR)

from code_query import CodeQuery  # noqa: E402
from indexer import index_repo  # noqa: E402

PROTOCOL_FALLBACK = "2025-06-18"
SERVER_INFO = {"name": "crivo-code-knowledge", "version": "0.3.0"}

_q = None
_index_error = None


def log(msg):
    """Diagnostico vai para stderr; stdout e exclusivo do JSON-RPC."""
    sys.stderr.write(f"[crivo-mcp] {msg}\n")
    sys.stderr.flush()


def _walk_up(start):
    """Primeiro ancestral de `start` com .git ou .aura, ou None."""
    cur = start
    while True:
        if os.path.isdir(os.path.join(cur, ".git")) or \
                os.path.isdir(os.path.join(cur, ".aura")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def find_root():
    """Raiz do repositorio, tolerante a ${workspaceFolder} nao expandido.

    Do repo, sobe a partir do proprio arquivo. Instalado via uvx o arquivo
    mora no cache do uv, entao o fallback e o diretorio de trabalho."""
    env = os.getenv("WORKSPACE_FOLDER")
    if env and not env.startswith("${") and os.path.isdir(env):
        return os.path.abspath(env)
    return (_walk_up(_HERE) or _walk_up(os.getcwd())
            or os.path.dirname(os.path.dirname(_HERE)))


def start_background_index():
    """Indexa em thread de fundo: nenhum passo de setup para o dev, e as
    consultas ja respondem (com aviso) enquanto o indice nao esta pronto."""
    def run():
        global _index_error
        try:
            log(f"indice pronto: {index_repo(_q.root, _q.db_path)}")
        except Exception as exc:  # nao derruba o servidor
            _index_error = str(exc)
            log(f"falha ao indexar: {exc}")

    threading.Thread(target=run, name="indexer", daemon=True).start()


# Catalogo: cada entrada mapeia o contrato MCP para um metodo do CodeQuery.
TOOLS = [
    {
        "name": "get_class_structure",
        "description": ("Estrutura completa de um tipo: arquivos (unificando "
                        "declaracoes partial), bases/interfaces, dependencias "
                        "injetadas com a implementacao concreta resolvida via "
                        "DI, e todos os membros."),
        "inputSchema": {
            "type": "object",
            "properties": {"class_name": {"type": "string",
                                          "description": "Nome simples do tipo."}},
            "required": ["class_name"],
        },
        "call": lambda a: _q.structure(a.get("class_name")),
    },
    {
        "name": "search_symbols",
        "description": ("Busca simbolos (tipos, metodos, propriedades, campos) "
                        "ranqueada por BM25, com quebra de camelCase."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "kind": {"type": "string",
                         "description": "class|interface|method|property|field|..."},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
        "call": lambda a: _q.symbols(a.get("query"), a.get("kind"),
                                     a.get("limit", 20)),
    },
    {
        "name": "search_code_terms",
        "description": ("Busca textual no codigo. O indice ranqueia os arquivos "
                        "e so os mais relevantes sao lidos; informa quantas "
                        "ocorrencias existem no total."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "extension": {"type": "string", "description": "ex.: cs, json"},
                "max_results": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
        "call": lambda a: _q.terms(a.get("query"), a.get("extension"),
                                   a.get("max_results", 20)),
    },
    {
        "name": "trace_impl",
        "description": ("Segue a cadeia de injecao de dependencia a partir de um "
                        "tipo ou interface, atravessando as interfaces via os "
                        "registros do container."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "type_name": {"type": "string"},
                "depth": {"type": "integer", "default": 3},
            },
            "required": ["type_name"],
        },
        "call": lambda a: _q.impl(a.get("type_name"), a.get("depth", 3)),
    },
    {
        "name": "index_status",
        "description": "Estado do indice: raiz, progresso e contagens.",
        "inputSchema": {"type": "object", "properties": {}},
        "call": lambda a: _q.status() + (f"\nerro: {_index_error}"
                                         if _index_error else ""),
    },
]
_BY_NAME = {t["name"]: t for t in TOOLS}


def _text_result(rid, text, is_error=False):
    return {"jsonrpc": "2.0", "id": rid, "result": {
        "content": [{"type": "text", "text": text or "(vazio)"}],
        "isError": is_error}}


def handle(req):
    method, rid = req.get("method"), req.get("id")

    if method == "initialize":
        pv = (req.get("params") or {}).get("protocolVersion") or PROTOCOL_FALLBACK
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": pv,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        }}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": [
            {k: t[k] for k in ("name", "description", "inputSchema")}
            for t in TOOLS]}}

    if method == "tools/call":
        params = req.get("params") or {}
        tool = _BY_NAME.get(params.get("name"))
        if tool is None:
            return _text_result(rid, f"tool desconhecida: {params.get('name')}",
                                True)
        try:
            return _text_result(rid, tool["call"](params.get("arguments") or {}))
        except Exception as exc:
            log(f"erro em {tool['name']}: {exc!r}")
            return _text_result(rid, f"{type(exc).__name__}: {exc}", True)

    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}

    if method and method.startswith("notifications/"):
        return None

    if rid is None:
        return None
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def main():
    global _q
    root = find_root()
    _q = CodeQuery(root)
    log(f"raiz={root}")
    log(f"tools={TOOLS_DIR}")
    log(f"indice={_q.db_path}")
    start_background_index()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
