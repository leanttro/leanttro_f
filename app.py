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
# ════════════════════════════════════════════════════════════

import calendar as calendar_lib
import json
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
    return dict(
        estados_dropdown=estados,
        tipo_label=TIPO_LABEL,
        estado=None,
        cidade=None,
        cidades=None,
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
_DOLAR_CACHE_TTL_SEGUNDOS = 900          # 15 minutos — cotação não precisa ser por segundo
_DOLAR_RETRY_COOLDOWN_SEGUNDOS = 180     # após falha (ex.: 429), espera 3 min antes de tentar de novo


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
        resp = requests.get(
            "https://economia.awesomeapi.com.br/json/last/USD-BRL",
            timeout=5,
        )
        resp.raise_for_status()
        bruto = resp.json()["USDBRL"]
        cotacao = {
            "erro": False,
            "compra": float(bruto["bid"]),
            "venda": float(bruto["ask"]),
            "variacao_pct": float(bruto["pctChange"]),
            "maxima": float(bruto["high"]),
            "minima": float(bruto["low"]),
            "atualizado_em": bruto["create_date"],  # "2027-01-05 14:32:10"
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
