# ════════════════════════════════════════════════════════════
#  app.py — feriados2027.com.br
#  App Flask INDEPENDENTE (processo/deploy próprio).
#  Banco COMPARTILHADO ("metro"), mas só mexe em tabelas com
#  prefixo feriado_ — nunca toca em nada de outro projeto.
#
#  Variáveis de ambiente esperadas (.env):
#      DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASS
# ════════════════════════════════════════════════════════════

import calendar as calendar_lib
from flask import Flask, render_template, g, abort, request, redirect, jsonify, Response
from datetime import date, timedelta
import os
import psycopg2
import psycopg2.extras
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


# ── Calculadora de emenda ────────────────────────────────────

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
    as páginas de estado + todas as páginas de cidade. Envie essa URL
    (BASE_URL/sitemap.xml) pro Google Search Console."""
    hoje = date.today().isoformat()

    urls = [{"loc": f"{BASE_URL}/", "changefreq": "daily", "priority": "1.0", "lastmod": hoje}]

    estados = query("SELECT feriado_estado_uf AS uf FROM feriado_estados ORDER BY feriado_estado_uf")
    for e in estados:
        uf = e["uf"].lower()
        urls.append({"loc": f"{BASE_URL}/{uf}/", "changefreq": "weekly", "priority": "0.8", "lastmod": hoje})

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
