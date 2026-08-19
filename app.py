# ════════════════════════════════════════════════════════════
#  app.py — feriados2027.com.br [VERSÃO COM CALCULADORA DE FÉRIAS + DÓLAR]
#  App Flask INDEPENDENTE (processo/deploy próprio).
#  Banco COMPARTILHADO ("metro"), mas só mexe em tabelas com
#  prefixo feriado_ — nunca toca em nada de outro projeto.
#
#  Variáveis de ambiente esperadas (.env):
#      DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASS
#
#  NOVO NESTA VERSÃO:
#  - Calculadora de férias: /calculadora-ferias/ (nacional) e
#    /<uf>/calculadora-ferias/ (por estado, com feriados estaduais)
#  - Cotação de dólar: /cotacao-dolar/ (AwesomeAPI, cache de 10 min)
#  - Dependência nova: requests (adicionar no requirements.txt)
#  - Seção /turismo/ (diretório de negócios locais):
#      /turismo/                                   → home da seção
#      /turismo/<categoria-slug>/                  → CANÔNICA: por categoria
#      /turismo/<categoria-slug>/<negocio-slug>/   → CANÔNICA: detalhe
#      /<uf>/<cidade-slug>/turismo/                → negócios da cidade
#      /<uf>/<cidade-slug>/turismo/<negocio-slug>/ → 301 pra canônica
#    Lê de feriado_negocios/feriado_categorias (cidade já resolvida via
#    ibge_code — sem fuzzy matching). "Perto de mim" com navigator.geo-
#    location fica todo no JS do template (turismo_negocios_secao.html).
# ════════════════════════════════════════════════════════════

import calendar as calendar_lib
import json
import math
import time
from flask import Flask, render_template, g, abort, request, redirect, jsonify, Response
from datetime import date, timedelta
import os
import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv()

app = Flask(__name__)
# Dokploy/Traefik faz o SSL termination e repassa pro container em HTTP puro.
# Sem isso, o Flask acha que toda requisição é http:// — quebrava o canonical.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.url_map.strict_slashes = False

ANO_PRINCIPAL = 2027  # ano em foco do projeto — usado como default nas páginas

# Domínio usado no sitemap.xml e no robots.txt. Pode ser sobrescrito
# via variável de ambiente BASE_URL no .env, se precisar.
BASE_URL = os.getenv("BASE_URL", "https://www.feriados2027.com.br").rstrip("/")

# Opções de dias de férias oferecidas na calculadora (dropdown/botões)
DIAS_FERIAS_OPCOES = [5, 10, 15, 20, 30]

# Quantos negócios por página nas listagens de /turismo/ (numerada, ?pagina=N)
NEGOCIOS_POR_PAGINA = 24


@app.before_request
def redirecionar_para_www():
    """Força feriados2027.com.br -> www.feriados2027.com.br (301), pra não
    ter conteúdo duplicado no Google.

    IMPORTANTE: só redireciona se a requisição veio pelo Traefik/Dokploy
    (tem o cabeçalho X-Forwarded-Host). Um healthcheck interno do Dokploy
    bate direto no container, sem esse cabeçalho — passa direto, sem
    redirect. Isso evita que o healthcheck "veja" um 301 em vez de um 200
    e derrube o serviço achando que caiu.
    """
    veio_pelo_proxy = request.headers.get("X-Forwarded-Host") is not None
    if not veio_pelo_proxy:
        return  # provável healthcheck interno — não mexe

    host = request.host
    if host.startswith("www."):
        return  # já está certo

    novo_host = f"www.{host}"
    nova_url = request.url.replace(host, novo_host, 1)
    return redirect(nova_url, code=301)

MESES_NOMES = [
    "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
]

TIPO_LABEL = {
    "nacional": "Nacional",
    "estadual": "Estadual",
    "municipal": "Municipal",
    "ponto_facultativo": "Ponto Facultativo",
    "comemorativa": "Data Comemorativa",
}


# ── Banco ─────────────────────────────────────────────────────

def get_db():
    if "db" not in g:
        g.db = psycopg2.connect(
            host=os.getenv("DB_HOST"),
            port=int(os.getenv("DB_PORT", 5432)),
            dbname=os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASS"),
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.close()


def query(sql, params=(), one=False):
    db = get_db()
    cur = db.cursor()
    cur.execute(sql, params)
    return cur.fetchone() if one else cur.fetchall()


# ── Contexto global (disponível em TODOS os templates) ───────
# Isso alimenta os dropdowns de estado/cidade no cabeçalho.
# As rotas podem sobrescrever 'estado', 'cidade' e 'cidades'
# passando esses nomes pro render_template — os valores daqui
# só valem como padrão (ex.: na home, onde nada está selecionado).

@app.context_processor
def inject_globais():
    estados = query(
        "SELECT feriado_estado_uf AS uf, feriado_estado_nome AS nome "
        "FROM feriado_estados ORDER BY feriado_estado_nome"
    )

    # Cotação do dólar no header, em todas as páginas. Isso NÃO chama a
    # API a cada request — só lê o cache em disco (mesmo cache usado
    # pela página /cotacao-dolar/), então é praticamente de graça.
    cotacao_header = _ler_cache_dolar_disco()
    cotacao_header = (cotacao_header or {}).get("cotacao")
    if not cotacao_header or cotacao_header.get("erro"):
        cotacao_header = None

    return dict(
        estados_dropdown=estados,
        tipo_label=TIPO_LABEL,
        estado=None,
        cidade=None,
        cidades=None,
        cotacao_header=cotacao_header,
    )


# ── Calendário mensal (12 meses, com feriados marcados) ──────

def gerar_calendario(ano, feriados):
    """Recebe a lista de feriados (dicts com 'data', 'nome', 'tipo') e
    devolve uma estrutura com os 12 meses já organizados em semanas
    (domingo a sábado), pronta pro template desenhar a grade."""
    feriados_por_dia = {}
    for f in feriados:
        feriados_por_dia.setdefault(f["data"], []).append(f)

    cal = calendar_lib.Calendar(firstweekday=6)  # 6 = semana começa no domingo

    meses = []
    for mes in range(1, 13):
        semanas = []
        for semana in cal.monthdayscalendar(ano, mes):
            linha = []
            for dia in semana:
                if dia == 0:
                    linha.append(None)
                else:
                    d = date(ano, mes, dia)
                    linha.append({"dia": dia, "feriados": feriados_por_dia.get(d, [])})
            semanas.append(linha)

        feriados_mes = sorted(
            (f for f in feriados if f["data"].month == mes),
            key=lambda f: f["data"],
        )
        meses.append({
            "nome": MESES_NOMES[mes - 1],
            "numero": mes,
            "semanas": semanas,
            "feriados": feriados_mes,
        })
    return meses


# ── Calculadora de emenda (pontes simples, já existente) ──────

def _pontes_do_ano(feriados, ano):
    oportunidades = []
    for f in feriados:
        d = f["data"]
        dia_semana = d.weekday()  # 0=segunda ... 6=domingo

        if dia_semana == 1:  # terça
            ponte = d - timedelta(days=1)
            oportunidades.append({
                "feriado": f, "tipo_ponte": "emenda",
                "dias_ponte": [ponte],
                "inicio_descanso": d - timedelta(days=3),
                "fim_descanso": d,
                "dias_gastos": 1,
                "dias_descanso_total": 4,
            })
        elif dia_semana == 3:  # quinta
            ponte = d + timedelta(days=1)
            oportunidades.append({
                "feriado": f, "tipo_ponte": "emenda",
                "dias_ponte": [ponte],
                "inicio_descanso": d,
                "fim_descanso": d + timedelta(days=3),
                "dias_gastos": 1,
                "dias_descanso_total": 4,
            })
        elif dia_semana == 0:  # segunda
            oportunidades.append({
                "feriado": f, "tipo_ponte": "natural",
                "dias_ponte": [],
                "inicio_descanso": d - timedelta(days=2),
                "fim_descanso": d,
                "dias_gastos": 0,
                "dias_descanso_total": 3,
            })
        elif dia_semana == 4:  # sexta
            oportunidades.append({
                "feriado": f, "tipo_ponte": "natural",
                "dias_ponte": [],
                "inicio_descanso": d,
                "fim_descanso": d + timedelta(days=2),
                "dias_gastos": 0,
                "dias_descanso_total": 3,
            })
        # quarta-feira: fora de propósito (emenda cara, 2 dias)

    oportunidades.sort(key=lambda o: o["feriado"]["data"])
    return oportunidades


# ── NOVO: Calculadora de férias (combinação ótima de dias) ────
# Estende o motor de pontes: em vez de olhar só 1 feriado por vez,
# varre o ano inteiro e acha os MELHORES períodos contínuos de
# descanso possíveis pra um orçamento de N dias de férias.
#
# Lógica: cada dia do ano é "livre" (fim de semana ou feriado) ou
# "útil" (dia que, se tirado de férias, é gasto do orçamento).
# Pra cada dia de início possível, estica o período o máximo que dá
# sem estourar o orçamento de N dias úteis gastos — é uma janela
# deslizante. No fim, pega os melhores períodos sem sobreposição.

def _dias_livres_do_ano(feriados, ano):
    """Set de todas as datas do ano que são fim de semana OU feriado."""
    feriado_datas = {f["data"] for f in feriados}
    livres = set()
    d = date(ano, 1, 1)
    fim_ano = date(ano, 12, 31)
    while d <= fim_ano:
        if d.weekday() >= 5 or d in feriado_datas:  # 5=sábado, 6=domingo
            livres.add(d)
        d += timedelta(days=1)
    return livres


def _melhores_periodos_ferias(feriados, ano, n_dias, max_resultados=5):
    """Devolve até `max_resultados` períodos contínuos (sem sobreposição
    entre eles) que maximizam o total de dias de descanso gastando no
    máximo `n_dias` dias úteis de férias em cada um."""
    livres = _dias_livres_do_ano(feriados, ano)
    inicio_ano = date(ano, 1, 1)
    total_dias = (date(ano, 12, 31) - inicio_ano).days + 1
    dias = [inicio_ano + timedelta(days=i) for i in range(total_dias)]

    candidatos = []
    for i in range(total_dias):
        gastos = 0
        j = i
        while j < total_dias:
            if dias[j] not in livres:
                if gastos + 1 > n_dias:
                    break
                gastos += 1
            j += 1
        fim_idx = j - 1
        if fim_idx < i:
            continue
        candidatos.append({
            "inicio": dias[i],
            "fim": dias[fim_idx],
            "dias_gastos": gastos,
            "dias_descanso_total": fim_idx - i + 1,
        })

    # Maior descanso primeiro; empatado, o que gasta menos dias de férias.
    candidatos.sort(key=lambda c: (c["dias_descanso_total"], -c["dias_gastos"]), reverse=True)

    selecionados = []
    for c in candidatos:
        sobrepoe = any(
            c["inicio"] <= s["fim"] and c["fim"] >= s["inicio"]
            for s in selecionados
        )
        if not sobrepoe:
            selecionados.append(c)
        if len(selecionados) >= max_resultados:
            break

    selecionados.sort(key=lambda c: c["inicio"])
    return selecionados


# ── NOVO: Cotação de dólar (AwesomeAPI, com cache em memória) ─
# Cache simples de 10 min: evita bater na API a cada request e
# deixa a página rápida mesmo sob tráfego. Se a API falhar, devolve
# {"erro": True} e o template mostra uma mensagem de fallback em vez
# de quebrar a página.

# IMPORTANTE: o Gunicorn roda vários workers (processos separados). Um
# cache em memória Python (dict comum) NÃO é compartilhado entre eles —
# cada worker teria seu próprio cache, multiplicando as chamadas à API
# pelo número de workers e estourando o rate limit (429) muito rápido.
# Por isso o cache é gravado em ARQUIVO, que é compartilhado por todos
# os processos do mesmo container.
_DOLAR_CACHE_PATH = "/tmp/feriados2027_cotacao_dolar_cache.json"
_DOLAR_CACHE_TTL_SEGUNDOS = 3600         # 60 minutos — a API-fonte (open.er-api.com) só atualiza 1x/dia mesmo
_DOLAR_RETRY_COOLDOWN_SEGUNDOS = 180     # após falha (ex.: 429), espera 3 min antes de tentar de novo

# Outras moedas mostradas na página /cotacao-dolar/ (convertidas pra BRL
# usando a mesma resposta da API — não gera nenhuma chamada extra).
# Chave = código ISO da moeda na resposta da API; valor = nome amigável.
_OUTRAS_MOEDAS = {
    "EUR": "Euro",
    "GBP": "Libra Esterlina",
    "ARS": "Peso Argentino",
}


def _ler_cache_dolar_disco():
    try:
        with open(_DOLAR_CACHE_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return None


def _gravar_cache_dolar_disco(entrada):
    # Escrita atômica (escreve num .tmp e renomeia) — evita arquivo
    # corrompido se dois workers gravarem ao mesmo tempo.
    tmp_path = _DOLAR_CACHE_PATH + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(entrada, f)
        os.replace(tmp_path, _DOLAR_CACHE_PATH)
    except Exception:
        app.logger.exception("Falha ao gravar cache de cotação em disco")


def _buscar_cotacao_dolar():
    agora = time.time()
    cache = _ler_cache_dolar_disco() or {}

    # Cache ainda válido — nem chama a API.
    if cache.get("cotacao") and (agora - cache.get("hora", 0) < _DOLAR_CACHE_TTL_SEGUNDOS):
        return cache["cotacao"]

    # Falhou recentemente (ex.: 429)? Respeita o cooldown e NÃO tenta de
    # novo — isso é o que impede vários workers de martelarem a API ao
    # mesmo tempo logo depois de um rate limit.
    ultima_falha = cache.get("ultima_falha")
    if ultima_falha and (agora - ultima_falha < _DOLAR_RETRY_COOLDOWN_SEGUNDOS):
        return cache["cotacao"] if cache.get("cotacao") else {"erro": True}

    try:
        # open.er-api.com: sem API key, sem rate limit agressivo (atualiza
        # 1x por dia, então pode ser consultada à vontade). Em troca, só
        # devolve um valor "médio" — não tem compra/venda separados,
        # nem máxima/mínima do dia, nem variação % (a AwesomeAPI dava
        # isso, mas estava derrubando o site com 429).
        resp = requests.get(
            "https://open.er-api.com/v6/latest/USD",
            timeout=5,
        )
        resp.raise_for_status()
        bruto = resp.json()
        if bruto.get("result") != "success":
            raise ValueError(f"resposta inesperada da API de câmbio: {bruto}")

        rates = bruto["rates"]
        valor_brl_por_usd = float(rates["BRL"])

        # A API dá tudo em relação a USD (1 USD = X BRL, 1 USD = Y EUR...).
        # Pra converter outra moeda direto pra BRL: BRL_por_USD / moeda_por_USD.
        # Ex.: se 1 USD = 5,20 BRL e 1 USD = 0,92 EUR, então 1 EUR = 5,20/0,92 BRL.
        outras_moedas = []
        for codigo, nome in _OUTRAS_MOEDAS.items():
            taxa_moeda_por_usd = rates.get(codigo)
            if not taxa_moeda_por_usd:
                continue
            outras_moedas.append({
                "codigo": codigo,
                "nome": nome,
                "valor": valor_brl_por_usd / float(taxa_moeda_por_usd),
            })

        cotacao = {
            "erro": False,
            "valor": valor_brl_por_usd,
            "atualizado_em": bruto.get("time_last_update_utc", ""),
            "outras_moedas": outras_moedas,
        }
        _gravar_cache_dolar_disco({"cotacao": cotacao, "hora": agora, "ultima_falha": None})
        return cotacao
    except Exception as e:
        # Loga a exceção real (com traceback) nos logs do container —
        # olhe a aba "Logs" do Dokploy procurando por essa mensagem.
        app.logger.exception("Falha ao buscar cotação do dólar: %s", e)

        # Marca a falha em disco (todos os workers passam a respeitar
        # o cooldown) e devolve o último cache válido, se houver, em
        # vez de mostrar erro pro usuário.
        _gravar_cache_dolar_disco({
            "cotacao": cache.get("cotacao"),
            "hora": cache.get("hora", 0),
            "ultima_falha": agora,
        })
        return cache["cotacao"] if cache.get("cotacao") else {"erro": True}


def _feriados_da_cidade(ibge_code, uf, ano):
    return query("""
        SELECT feriado_nome AS nome, feriado_data AS data, feriado_tipo AS tipo,
               feriado_descricao_seo AS descricao_seo
        FROM feriado_feriados
        WHERE feriado_ano = %s AND (
            feriado_tipo = 'nacional'
            OR feriado_tipo = 'ponto_facultativo'
            OR feriado_tipo = 'comemorativa'
            OR (feriado_tipo = 'estadual' AND feriado_uf = %s)
            OR (feriado_tipo = 'municipal' AND feriado_ibge_code = %s)
        )
        ORDER BY feriado_data
    """, (ano, uf, ibge_code))


def _cidades_do_estado(uf):
    return query(
        """SELECT feriado_municipio_ibge_code AS ibge_code, feriado_municipio_nome AS nome,
                  feriado_municipio_slug AS slug
           FROM feriado_municipios WHERE feriado_municipio_uf = %s ORDER BY feriado_municipio_nome""",
        (uf,),
    )


# ── Turismo (seção /turismo/) ─────────────────────────────────
# Negócios já vêm com cidade RESOLVIDA via ibge_code (FK pra
# feriado_municipios) — nada de fuzzy matching de nome de cidade aqui,
# diferente do hub multi-tenant que serviu de inspiração de layout.
#
# Todas as queries abaixo só trazem negócio ATIVO de categoria ATIVA
# (n.ativo = true AND c.ativo = true) — um negócio desativado, ou
# de categoria desativada, simplesmente some do site sem precisar
# apagar a linha do banco.

_NEGOCIO_CAMPOS_SQL = """
    n.feriado_negocio_id AS negocio_id, n.feriado_negocio_nome AS negocio_nome,
    n.feriado_negocio_slug AS negocio_slug,
    n.feriado_negocio_descricao AS negocio_descricao, n.feriado_negocio_foto_url AS negocio_foto_url,
    n.feriado_negocio_endereco AS negocio_endereco, n.feriado_negocio_bairro AS negocio_bairro,
    n.feriado_negocio_cidade AS negocio_cidade_texto, n.feriado_negocio_ibge_code AS negocio_ibge_code,
    n.feriado_negocio_lat AS negocio_lat, n.feriado_negocio_lng AS negocio_lng,
    n.feriado_negocio_whatsapp AS negocio_whatsapp, n.feriado_negocio_telefone AS negocio_telefone,
    n.feriado_negocio_site_url AS negocio_site_url, n.feriado_negocio_instagram AS negocio_instagram,
    c.feriado_categoria_id AS categoria_id, c.feriado_categoria_nome AS categoria_nome,
    c.feriado_categoria_slug AS categoria_slug,
    c.feriado_categoria_icone_url AS categoria_icone_url,
    m.feriado_municipio_nome AS cidade_nome, m.feriado_municipio_slug AS cidade_slug,
    m.feriado_municipio_uf AS cidade_uf
"""

_NEGOCIO_FROM_SQL = """
    FROM feriado_negocios n
    JOIN feriado_categorias c ON c.feriado_categoria_id = n.feriado_negocio_categoria_id
    LEFT JOIN feriado_municipios m ON m.feriado_municipio_ibge_code = n.feriado_negocio_ibge_code
    WHERE n.feriado_negocio_ativo = true AND c.feriado_categoria_ativo = true
"""


def _categorias_turismo():
    return query("""
        SELECT feriado_categoria_id AS id, feriado_categoria_nome AS nome,
               feriado_categoria_slug AS slug, feriado_categoria_icone_url AS icone_url
        FROM feriado_categorias
        WHERE feriado_categoria_ativo = true
        ORDER BY feriado_categoria_nome
    """)


def _contar_negocios_turismo(categoria_id=None, ibge_code=None):
    sql = "SELECT COUNT(*) AS total " + _NEGOCIO_FROM_SQL
    params = []
    if categoria_id is not None:
        sql += " AND n.feriado_negocio_categoria_id = %s"
        params.append(categoria_id)
    if ibge_code is not None:
        sql += " AND n.feriado_negocio_ibge_code = %s"
        params.append(ibge_code)
    linha = query(sql, tuple(params), one=True)
    return linha["total"] if linha else 0


def _listar_negocios_turismo(categoria_id=None, ibge_code=None, pagina=1,
                              por_pagina=NEGOCIOS_POR_PAGINA, excluir_id=None):
    """Lista negócios ordenados por nome (comportamento padrão — a
    ordenação por distância acontece no CLIENTE, via JS, depois que o
    usuário libera geolocalização; ver _turismo_negocios_secao.html)."""
    sql = "SELECT " + _NEGOCIO_CAMPOS_SQL + _NEGOCIO_FROM_SQL
    params = []
    if categoria_id is not None:
        sql += " AND n.feriado_negocio_categoria_id = %s"
        params.append(categoria_id)
    if ibge_code is not None:
        sql += " AND n.feriado_negocio_ibge_code = %s"
        params.append(ibge_code)
    if excluir_id is not None:
        sql += " AND n.feriado_negocio_id != %s"
        params.append(excluir_id)
    sql += " ORDER BY n.feriado_negocio_nome LIMIT %s OFFSET %s"
    params += [por_pagina, (pagina - 1) * por_pagina]
    return query(sql, tuple(params))


def _negocio_por_categoria_e_slug(categoria_slug, negocio_slug):
    sql = "SELECT " + _NEGOCIO_CAMPOS_SQL + _NEGOCIO_FROM_SQL
    sql += " AND c.feriado_categoria_slug = %s AND n.feriado_negocio_slug = %s"
    return query(sql, (categoria_slug, negocio_slug), one=True)


def _negocio_por_ibge_e_slug(ibge_code, negocio_slug):
    """Usado só pelo redirect /<uf>/<cidade>/turismo/<negocio>/ — acha o
    negócio pela cidade (ibge_code) + slug, pra montar a URL canônica
    por categoria."""
    sql = "SELECT " + _NEGOCIO_CAMPOS_SQL + _NEGOCIO_FROM_SQL
    sql += " AND n.feriado_negocio_ibge_code = %s AND n.feriado_negocio_slug = %s"
    return query(sql, (ibge_code, negocio_slug), one=True)


def _negocios_para_json(negocios):
    """Serializa os campos que o front usa pro carrossel/grid + cálculo
    de distância (cardHTML() no JS). Mantém só o necessário — nada de
    descrição longa aqui, isso fica só na página de detalhe."""
    return [
        {
            "nome": n["negocio_nome"],
            "slug": n["negocio_slug"],
            "categoriaSlug": n["categoria_slug"],
            "categoriaNome": n["categoria_nome"],
            "categoriaIcone": n["categoria_icone_url"],
            "fotoUrl": n["negocio_foto_url"],
            "bairro": n["negocio_bairro"],
            "cidadeNome": n["cidade_nome"] or n["negocio_cidade_texto"],
            "lat": float(n["negocio_lat"]) if n["negocio_lat"] is not None else None,
            "lng": float(n["negocio_lng"]) if n["negocio_lng"] is not None else None,
        }
        for n in negocios
    ]


# ── Busca (estados, cidades e feriados) ───────────────────────
# Usada pela rota /buscar/ (página de resultados) e pelo
# autocomplete do header (/api/autocomplete/).

def _buscar_tudo(termo, limite=8):
    """Procura o termo em estados, cidades e feriados do ano principal.
    Usa ILIKE (case-insensitive) — simples e não depende de nenhuma
    extensão especial do Postgres."""
    termo = (termo or "").strip()
    if not termo:
        return {"estados": [], "cidades": [], "feriados": []}

    padrao = f"%{termo}%"

    estados = query("""
        SELECT feriado_estado_uf AS uf, feriado_estado_nome AS nome
        FROM feriado_estados
        WHERE feriado_estado_nome ILIKE %s OR feriado_estado_uf ILIKE %s
        ORDER BY feriado_estado_nome
        LIMIT %s
    """, (padrao, padrao, limite))

    cidades = query("""
        SELECT feriado_municipio_ibge_code AS ibge_code, feriado_municipio_nome AS nome,
               feriado_municipio_slug AS slug, feriado_municipio_uf AS uf
        FROM feriado_municipios
        WHERE feriado_municipio_nome ILIKE %s
        ORDER BY feriado_municipio_nome
        LIMIT %s
    """, (padrao, limite))

    feriados = query("""
        SELECT feriado_nome AS nome, feriado_data AS data, feriado_tipo AS tipo,
               feriado_uf AS uf, feriado_ibge_code AS ibge_code,
               feriado_descricao_seo AS descricao_seo
        FROM feriado_feriados
        WHERE feriado_ano = %s AND feriado_nome ILIKE %s
        ORDER BY feriado_data
        LIMIT %s
    """, (ANO_PRINCIPAL, padrao, limite))

    return {"estados": estados, "cidades": cidades, "feriados": feriados}


# ── Rotas ─────────────────────────────────────────────────────

@app.route("/")
def index():
    estados = query("SELECT feriado_estado_uf AS uf, feriado_estado_nome AS nome FROM feriado_estados ORDER BY feriado_estado_nome")

    # Inclui descricao_seo aqui também — antes só vinha nome/data/tipo,
    # e por isso o popover de descrição não tinha o que mostrar na home.
    feriados_nacionais = query("""
        SELECT feriado_nome AS nome, feriado_data AS data, feriado_tipo AS tipo,
               feriado_descricao_seo AS descricao_seo
        FROM feriado_feriados
        WHERE feriado_ano = %s AND (
            feriado_tipo = 'nacional'
            OR feriado_tipo = 'ponto_facultativo'
            OR feriado_tipo = 'comemorativa'
        )
        ORDER BY feriado_data
    """, (ANO_PRINCIPAL,))

    calendario = gerar_calendario(ANO_PRINCIPAL, feriados_nacionais)

    return render_template("index.html", estados=estados, ano=ANO_PRINCIPAL, calendario=calendario)


@app.route("/buscar/")
def buscar():
    """Busca por estado, cidade ou feriado (usada pelo input do header
    e pela busca em destaque da home). Redireciona direto quando há um
    único resultado óbvio; senão mostra a página de resultados."""
    termo = request.args.get("q", "").strip()
    if not termo:
        return redirect("/")

    resultados = _buscar_tudo(termo, limite=12)
    total = len(resultados["estados"]) + len(resultados["cidades"]) + len(resultados["feriados"])

    if total == 0:
        return render_template("busca_resultados.html", termo=termo, resultados=resultados, ano=ANO_PRINCIPAL)

    # Resultado único e inequívoco → vai direto pra página, sem passar
    # pela tela de resultados.
    if total == 1:
        if resultados["estados"]:
            uf = resultados["estados"][0]["uf"].lower()
            return redirect(f"/{uf}/")
        if resultados["cidades"]:
            c = resultados["cidades"][0]
            return redirect(f"/{c['uf'].lower()}/{c['slug']}/")
        if resultados["feriados"]:
            f = resultados["feriados"][0]
            if f.get("uf"):
                return redirect(f"/{f['uf'].lower()}/")
            return redirect("/")

    return render_template("busca_resultados.html", termo=termo, resultados=resultados, ano=ANO_PRINCIPAL)


@app.route("/api/autocomplete/")
def api_autocomplete():
    """Sugestões em JSON pro campo de busca rápida do header
    (dropdown de autocomplete enquanto o usuário digita)."""
    termo = request.args.get("q", "").strip()
    if len(termo) < 2:
        return jsonify({"resultados": []})

    dados = _buscar_tudo(termo, limite=5)
    sugestoes = []

    for e in dados["estados"]:
        sugestoes.append({
            "tipo": "estado",
            "label": e["nome"],
            "url": f"/{e['uf'].lower()}/",
        })

    for c in dados["cidades"]:
        sugestoes.append({
            "tipo": "cidade",
            "label": f"{c['nome']} — {c['uf']}",
            "url": f"/{c['uf'].lower()}/{c['slug']}/",
        })

    for f in dados["feriados"]:
        url = f"/{f['uf'].lower()}/" if f.get("uf") else "/"
        sugestoes.append({
            "tipo": "feriado",
            "label": f"{f['nome']} ({f['data'].strftime('%d/%m')})",
            "url": url,
        })

    return jsonify({"resultados": sugestoes[:10]})


@app.route("/api/autocomplete_cidades/<uf>")
def api_autocomplete_cidades(uf):
    """Autocomplete de cidades dentro de um estado específico.
    Usado na página de estado pra filtrar as 1000+ cidades."""
    uf = uf.upper()
    termo = request.args.get("q", "").strip()

    # Valida se o UF existe
    estado = query(
        "SELECT feriado_estado_uf AS uf FROM feriado_estados WHERE feriado_estado_uf = %s",
        (uf,), one=True
    )
    if not estado:
        return jsonify({"resultados": []})

    if len(termo) < 1:
        # Se não digitar nada, retorna as 20 primeiras cidades do estado
        cidades = query("""
            SELECT feriado_municipio_nome AS nome, feriado_municipio_slug AS slug
            FROM feriado_municipios
            WHERE feriado_municipio_uf = %s
            ORDER BY feriado_municipio_nome
            LIMIT 20
        """, (uf,))
    else:
        # Se digitar algo, filtra por ILIKE
        padrao = f"%{termo}%"
        cidades = query("""
            SELECT feriado_municipio_nome AS nome, feriado_municipio_slug AS slug
            FROM feriado_municipios
            WHERE feriado_municipio_uf = %s AND feriado_municipio_nome ILIKE %s
            ORDER BY feriado_municipio_nome
            LIMIT 50
        """, (uf, padrao))

    resultados = [
        {
            "label": c["nome"],
            "slug": c["slug"],
            "url": f"/{uf.lower()}/{c['slug']}/"
        }
        for c in cidades
    ]

    return jsonify({"resultados": resultados})


# ── NOVO: Calculadora de férias — nacional ─────────────────────
# IMPORTANTE: essa rota precisa estar registrada ANTES de qualquer
# rota genérica /<algo>/ que possa colidir — mas como "calculadora-
# -ferias" e "cotacao-dolar" são segmentos ESTÁTICOS (sem <variável>),
# o Werkzeug já prioriza eles automaticamente sobre /<uf>/, então a
# ordem de registro aqui não importa na prática. Deixei nesta posição
# só por organização.

@app.route("/calculadora-ferias/")
def calculadora_ferias():
    n_dias = request.args.get("dias", default=10, type=int)
    if n_dias not in DIAS_FERIAS_OPCOES:
        n_dias = 10

    # Escopo nacional: só feriados que valem em todo o Brasil.
    feriados = query("""
        SELECT feriado_nome AS nome, feriado_data AS data, feriado_tipo AS tipo,
               feriado_descricao_seo AS descricao_seo
        FROM feriado_feriados
        WHERE feriado_ano = %s AND feriado_tipo IN ('nacional', 'ponto_facultativo')
        ORDER BY feriado_data
    """, (ANO_PRINCIPAL,))

    periodos = _melhores_periodos_ferias(feriados, ANO_PRINCIPAL, n_dias)

    return render_template(
        "calculadora_ferias.html",
        ano=ANO_PRINCIPAL,
        n_dias=n_dias,
        dias_opcoes=DIAS_FERIAS_OPCOES,
        periodos=periodos,
        estado=None,
        escopo_nome="Brasil",
    )


# ── NOVO: Calculadora de férias — por estado ───────────────────

@app.route("/<uf>/calculadora-ferias/")
def calculadora_ferias_estado(uf):
    uf = uf.upper()
    estado = query(
        "SELECT feriado_estado_uf AS uf, feriado_estado_nome AS nome FROM feriado_estados WHERE feriado_estado_uf = %s",
        (uf,), one=True,
    )
    if not estado:
        abort(404)

    n_dias = request.args.get("dias", default=10, type=int)
    if n_dias not in DIAS_FERIAS_OPCOES:
        n_dias = 10

    # Nacional + estadual do UF (mesma regra usada na página de estado).
    feriados = query("""
        SELECT feriado_nome AS nome, feriado_data AS data, feriado_tipo AS tipo,
               feriado_descricao_seo AS descricao_seo
        FROM feriado_feriados
        WHERE feriado_ano = %s AND (
            feriado_tipo IN ('nacional', 'ponto_facultativo')
            OR (feriado_tipo = 'estadual' AND feriado_uf = %s)
        )
        ORDER BY feriado_data
    """, (ANO_PRINCIPAL, uf))

    cidades = _cidades_do_estado(uf)
    periodos = _melhores_periodos_ferias(feriados, ANO_PRINCIPAL, n_dias)

    return render_template(
        "calculadora_ferias.html",
        ano=ANO_PRINCIPAL,
        n_dias=n_dias,
        dias_opcoes=DIAS_FERIAS_OPCOES,
        periodos=periodos,
        estado=estado,
        cidades=cidades,
        escopo_nome=estado["nome"],
    )


# ── NOVO: Cotação de dólar ──────────────────────────────────────

@app.route("/cotacao-dolar/")
def cotacao_dolar():
    cotacao = _buscar_cotacao_dolar()
    return render_template(
        "cotacao_dolar.html",
        ano=ANO_PRINCIPAL,
        cotacao=cotacao,
        hoje=date.today(),
    )


@app.route("/api/cotacao-dolar/")
def api_cotacao_dolar():
    """Endpoint JSON puro da cotação — dá pra usar depois num widget
    fixo no header, ou em outras páginas, sem duplicar a chamada à API."""
    return jsonify(_buscar_cotacao_dolar())


@app.route("/<uf>/")
def pagina_estado(uf):
    uf = uf.upper()
    estado = query(
        "SELECT feriado_estado_uf AS uf, feriado_estado_nome AS nome FROM feriado_estados WHERE feriado_estado_uf = %s",
        (uf,), one=True,
    )
    if not estado:
        abort(404)

    cidades = _cidades_do_estado(uf)

    feriados = query("""
        SELECT feriado_nome AS nome, feriado_data AS data, feriado_tipo AS tipo,
               feriado_descricao_seo AS descricao_seo
        FROM feriado_feriados
        WHERE feriado_ano = %s AND (
            feriado_tipo = 'nacional'
            OR feriado_tipo = 'ponto_facultativo'
            OR feriado_tipo = 'comemorativa'
            OR (feriado_tipo = 'estadual' AND feriado_uf = %s)
        )
        ORDER BY feriado_data
    """, (ANO_PRINCIPAL, uf))
    pontes = _pontes_do_ano(feriados, ANO_PRINCIPAL)
    calendario = gerar_calendario(ANO_PRINCIPAL, feriados)

    return render_template(
        "estado.html", estado=estado, cidades=cidades,
        feriados=feriados, pontes=pontes, ano=ANO_PRINCIPAL,
        calendario=calendario,
    )


@app.route("/<uf>/<cidade_slug>/")
def pagina_cidade(uf, cidade_slug):
    uf = uf.upper()
    estado = query(
        "SELECT feriado_estado_uf AS uf, feriado_estado_nome AS nome FROM feriado_estados WHERE feriado_estado_uf = %s",
        (uf,), one=True,
    )
    if not estado:
        abort(404)

    cidade = query(
        """SELECT feriado_municipio_ibge_code AS ibge_code, feriado_municipio_nome AS nome,
                  feriado_municipio_slug AS slug, feriado_municipio_uf AS uf
           FROM feriado_municipios WHERE feriado_municipio_uf = %s AND feriado_municipio_slug = %s""",
        (uf, cidade_slug), one=True,
    )
    if not cidade:
        abort(404)

    cidades = _cidades_do_estado(uf)  # pro dropdown de cidade continuar funcionando aqui também

    feriados = _feriados_da_cidade(cidade["ibge_code"], uf, ANO_PRINCIPAL)
    pontes = _pontes_do_ano(feriados, ANO_PRINCIPAL)
    calendario = gerar_calendario(ANO_PRINCIPAL, feriados)

    return render_template(
        "cidade.html", estado=estado, cidade=cidade, cidades=cidades,
        feriados=feriados, pontes=pontes, ano=ANO_PRINCIPAL,
        calendario=calendario,
    )


# ── NOVO: Turismo — /turismo/ ──────────────────────────────────
# Estrutura de URL (ver contexto do projeto pra decisão completa):
#   /turismo/                                   → home da seção
#   /turismo/<categoria-slug>/                  → CANÔNICA: listagem por categoria
#   /turismo/<categoria-slug>/<negocio-slug>/   → CANÔNICA: detalhe do negócio
#   /<uf>/<cidade-slug>/turismo/                → negócios daquela cidade
#   /<uf>/<cidade-slug>/turismo/<negocio-slug>/ → redirect 301 pra canônica
#
# A cidade é uma forma de NAVEGAR até o negócio; a URL indexada (a que
# entra no sitemap, canonical, JSON-LD etc.) é sempre a de categoria.

@app.route("/turismo/")
def turismo_index():
    pagina = request.args.get("pagina", default=1, type=int)
    if pagina < 1:
        pagina = 1

    total = _contar_negocios_turismo()
    total_paginas = max(1, math.ceil(total / NEGOCIOS_POR_PAGINA))
    pagina = min(pagina, total_paginas)

    categorias = _categorias_turismo()
    negocios = _listar_negocios_turismo(pagina=pagina)

    return render_template(
        "turismo_index.html",
        ano=ANO_PRINCIPAL,
        categorias=categorias,
        categoria_ativa=None,
        negocios=negocios,
        negocios_json=json.dumps(_negocios_para_json(negocios)),
        pagina=pagina,
        total_paginas=total_paginas,
        total_negocios=total,
        url_base_paginacao="/turismo/",
        secao_id="home",
        mostrar_filtro=True,
    )


@app.route("/turismo/<categoria_slug>/")
def turismo_categoria(categoria_slug):
    categoria = query(
        """SELECT feriado_categoria_id AS id, feriado_categoria_nome AS nome,
                  feriado_categoria_slug AS slug, feriado_categoria_icone_url AS icone_url
           FROM feriado_categorias WHERE feriado_categoria_slug = %s AND feriado_categoria_ativo = true""",
        (categoria_slug,), one=True,
    )
    if not categoria:
        abort(404)

    pagina = request.args.get("pagina", default=1, type=int)
    if pagina < 1:
        pagina = 1

    total = _contar_negocios_turismo(categoria_id=categoria["id"])
    total_paginas = max(1, math.ceil(total / NEGOCIOS_POR_PAGINA))
    pagina = min(pagina, total_paginas)

    categorias = _categorias_turismo()
    negocios = _listar_negocios_turismo(categoria_id=categoria["id"], pagina=pagina)

    return render_template(
        "turismo_categoria.html",
        ano=ANO_PRINCIPAL,
        categoria=categoria,
        categorias=categorias,
        categoria_ativa=categoria,
        negocios=negocios,
        negocios_json=json.dumps(_negocios_para_json(negocios)),
        pagina=pagina,
        total_paginas=total_paginas,
        total_negocios=total,
        url_base_paginacao=f"/turismo/{categoria_slug}/",
        secao_id="categoria",
        mostrar_filtro=True,
    )


def _json_ld_negocio(negocio):
    """Monta o LocalBusiness (+ breadcrumb) em Python — mais seguro do
    que montar JSON à mão no template, com campos opcionais que podem
    ou não estar preenchidos no banco."""
    url_canonica = f"{BASE_URL}/turismo/{negocio['categoria_slug']}/{negocio['negocio_slug']}/"

    endereco = {"@type": "PostalAddress", "addressCountry": "BR"}
    if negocio["negocio_endereco"]:
        endereco["streetAddress"] = negocio["negocio_endereco"]
    if negocio["cidade_nome"]:
        endereco["addressLocality"] = negocio["cidade_nome"]
    if negocio["cidade_uf"]:
        endereco["addressRegion"] = negocio["cidade_uf"]

    dados = {
        "@type": "LocalBusiness",
        "name": negocio["negocio_nome"],
        "url": url_canonica,
        "address": endereco,
    }
    if negocio["negocio_descricao"]:
        dados["description"] = negocio["negocio_descricao"]
    if negocio["negocio_foto_url"]:
        dados["image"] = negocio["negocio_foto_url"]
    if negocio["negocio_telefone"]:
        dados["telephone"] = negocio["negocio_telefone"]
    if negocio["negocio_lat"] is not None and negocio["negocio_lng"] is not None:
        dados["geo"] = {
            "@type": "GeoCoordinates",
            "latitude": float(negocio["negocio_lat"]),
            "longitude": float(negocio["negocio_lng"]),
        }

    breadcrumb = {
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Início", "item": f"{BASE_URL}/"},
            {"@type": "ListItem", "position": 2, "name": "Turismo", "item": f"{BASE_URL}/turismo/"},
            {"@type": "ListItem", "position": 3, "name": negocio["categoria_nome"],
             "item": f"{BASE_URL}/turismo/{negocio['categoria_slug']}/"},
            {"@type": "ListItem", "position": 4, "name": negocio["negocio_nome"], "item": url_canonica},
        ],
    }

    # "@graph" junta os dois blocos (LocalBusiness + BreadcrumbList) num
    # único <script type="application/ld+json"> válido.
    return json.dumps({"@context": "https://schema.org", "@graph": [dados, breadcrumb]})


@app.route("/turismo/<categoria_slug>/<negocio_slug>/")
def turismo_negocio(categoria_slug, negocio_slug):
    negocio = _negocio_por_categoria_e_slug(categoria_slug, negocio_slug)
    if not negocio:
        abort(404)

    similares = _listar_negocios_turismo(
        categoria_id=negocio["categoria_id"], pagina=1, por_pagina=4,
        excluir_id=negocio["negocio_id"],
    )

    return render_template(
        "turismo_negocio.html",
        ano=ANO_PRINCIPAL,
        negocio=negocio,
        similares=similares,
        json_ld_negocio=_json_ld_negocio(negocio),
    )


@app.route("/<uf>/<cidade_slug>/turismo/")
def pagina_cidade_turismo(uf, cidade_slug):
    uf = uf.upper()
    estado = query(
        "SELECT feriado_estado_uf AS uf, feriado_estado_nome AS nome FROM feriado_estados WHERE feriado_estado_uf = %s",
        (uf,), one=True,
    )
    if not estado:
        abort(404)

    cidade = query(
        """SELECT feriado_municipio_ibge_code AS ibge_code, feriado_municipio_nome AS nome,
                  feriado_municipio_slug AS slug, feriado_municipio_uf AS uf
           FROM feriado_municipios WHERE feriado_municipio_uf = %s AND feriado_municipio_slug = %s""",
        (uf, cidade_slug), one=True,
    )
    if not cidade:
        abort(404)

    cidades = _cidades_do_estado(uf)  # pro dropdown de cidade do header

    pagina = request.args.get("pagina", default=1, type=int)
    if pagina < 1:
        pagina = 1

    total = _contar_negocios_turismo(ibge_code=cidade["ibge_code"])
    total_paginas = max(1, math.ceil(total / NEGOCIOS_POR_PAGINA))
    pagina = min(pagina, total_paginas)

    negocios = _listar_negocios_turismo(ibge_code=cidade["ibge_code"], pagina=pagina)

    return render_template(
        "cidade_turismo.html",
        ano=ANO_PRINCIPAL,
        estado=estado, cidade=cidade, cidades=cidades,
        negocios=negocios,
        negocios_json=json.dumps(_negocios_para_json(negocios)),
        pagina=pagina,
        total_paginas=total_paginas,
        total_negocios=total,
        url_base_paginacao=f"/{uf.lower()}/{cidade_slug}/turismo/",
        secao_id="cidade",
        mostrar_filtro=False,
        categorias=None,
        categoria_ativa=None,
    )


@app.route("/<uf>/<cidade_slug>/turismo/<negocio_slug>/")
def redirect_negocio_por_cidade(uf, cidade_slug, negocio_slug):
    """Cidade é só um caminho de navegação — a URL que fica indexada é
    sempre a canônica por categoria. 301 permanente pra não espalhar
    conteúdo duplicado do mesmo negócio em duas URLs diferentes."""
    uf = uf.upper()
    cidade = query(
        """SELECT feriado_municipio_ibge_code AS ibge_code
           FROM feriado_municipios WHERE feriado_municipio_uf = %s AND feriado_municipio_slug = %s""",
        (uf, cidade_slug), one=True,
    )
    if not cidade:
        abort(404)

    negocio = _negocio_por_ibge_e_slug(cidade["ibge_code"], negocio_slug)
    if not negocio:
        abort(404)

    return redirect(f"/turismo/{negocio['categoria_slug']}/{negocio['negocio_slug']}/", code=301)


# ── SEO técnico: sitemap.xml e robots.txt ─────────────────────

@app.route("/sitemap.xml")
def sitemap():
    """Sitemap gerado dinamicamente a partir do banco: home + todas
    as páginas de estado + todas as páginas de cidade + calculadora
    de férias (nacional e por estado) + cotação de dólar. Envie essa
    URL (BASE_URL/sitemap.xml) pro Google Search Console."""
    hoje = date.today().isoformat()

    urls = [{"loc": f"{BASE_URL}/", "changefreq": "daily", "priority": "1.0", "lastmod": hoje}]

    # NOVO: calculadora de férias nacional + cotação de dólar
    urls.append({"loc": f"{BASE_URL}/calculadora-ferias/", "changefreq": "weekly", "priority": "0.9", "lastmod": hoje})
    urls.append({"loc": f"{BASE_URL}/cotacao-dolar/", "changefreq": "daily", "priority": "0.7", "lastmod": hoje})

    estados = query("SELECT feriado_estado_uf AS uf FROM feriado_estados ORDER BY feriado_estado_uf")
    for e in estados:
        uf = e["uf"].lower()
        urls.append({"loc": f"{BASE_URL}/{uf}/", "changefreq": "weekly", "priority": "0.8", "lastmod": hoje})
        # NOVO: calculadora de férias por estado
        urls.append({"loc": f"{BASE_URL}/{uf}/calculadora-ferias/", "changefreq": "weekly", "priority": "0.7", "lastmod": hoje})

    cidades = query(
        """SELECT feriado_municipio_uf AS uf, feriado_municipio_slug AS slug
           FROM feriado_municipios ORDER BY feriado_municipio_uf, feriado_municipio_slug"""
    )
    for c in cidades:
        uf = c["uf"].lower()
        urls.append({
            "loc": f"{BASE_URL}/{uf}/{c['slug']}/",
            "changefreq": "weekly",
            "priority": "0.6",
            "lastmod": hoje,
        })

    # NOVO: seção /turismo/ — home + páginas de categoria + páginas de
    # negócio, todas na URL CANÔNICA (por categoria). As URLs de negócio
    # acessadas pelo caminho da cidade nunca entram aqui, pois só
    # existem pra dar 301 pra cá — colocá-las no sitemap seria mandar
    # o Google indexar uma URL que a própria página descarta.
    urls.append({"loc": f"{BASE_URL}/turismo/", "changefreq": "weekly", "priority": "0.8", "lastmod": hoje})

    categorias_turismo = _categorias_turismo()
    for cat in categorias_turismo:
        urls.append({
            "loc": f"{BASE_URL}/turismo/{cat['slug']}/",
            "changefreq": "weekly",
            "priority": "0.7",
            "lastmod": hoje,
        })

    negocios_sitemap = query("""
        SELECT n.feriado_negocio_slug AS negocio_slug, c.feriado_categoria_slug AS categoria_slug
        FROM feriado_negocios n
        JOIN feriado_categorias c ON c.feriado_categoria_id = n.feriado_negocio_categoria_id
        WHERE n.feriado_negocio_ativo = true AND c.feriado_categoria_ativo = true
        ORDER BY c.feriado_categoria_slug, n.feriado_negocio_slug
    """)
    for n in negocios_sitemap:
        urls.append({
            "loc": f"{BASE_URL}/turismo/{n['categoria_slug']}/{n['negocio_slug']}/",
            "changefreq": "monthly",
            "priority": "0.6",
            "lastmod": hoje,
        })

    # Páginas de turismo por cidade — só entram no sitemap as cidades
    # que já têm pelo menos 1 negócio ativo (evita listar centenas de
    # páginas vazias pro Google rastrear à toa).
    cidades_com_turismo = query("""
        SELECT DISTINCT m.feriado_municipio_uf AS uf, m.feriado_municipio_slug AS slug
        FROM feriado_negocios n
        JOIN feriado_categorias c ON c.feriado_categoria_id = n.feriado_negocio_categoria_id
        JOIN feriado_municipios m ON m.feriado_municipio_ibge_code = n.feriado_negocio_ibge_code
        WHERE n.feriado_negocio_ativo = true AND c.feriado_categoria_ativo = true
        ORDER BY m.feriado_municipio_uf, m.feriado_municipio_slug
    """)
    for c in cidades_com_turismo:
        uf = c["uf"].lower()
        urls.append({
            "loc": f"{BASE_URL}/{uf}/{c['slug']}/turismo/",
            "changefreq": "weekly",
            "priority": "0.5",
            "lastmod": hoje,
        })

    xml_parts = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u in urls:
        xml_parts.append(
            "<url>"
            f"<loc>{u['loc']}</loc>"
            f"<lastmod>{u['lastmod']}</lastmod>"
            f"<changefreq>{u['changefreq']}</changefreq>"
            f"<priority>{u['priority']}</priority>"
            "</url>"
        )
    xml_parts.append("</urlset>")

    return Response("".join(xml_parts), mimetype="application/xml")


@app.route("/robots.txt")
def robots():
    """robots.txt simples, liberando tudo pro Google e apontando pro sitemap."""
    conteudo = (
        "User-agent: *\n"
        "Allow: /\n"
        f"Sitemap: {BASE_URL}/sitemap.xml\n"
    )
    return Response(conteudo, mimetype="text/plain")


if __name__ == "__main__":
    app.run(debug=True, port=5001)
