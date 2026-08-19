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
from flask import Flask, render_template, g, abort
from datetime import date, timedelta
import os
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.url_map.strict_slashes = False

ANO_PRINCIPAL = 2027  # ano em foco do projeto — usado como default nas páginas

MESES_NOMES = [
    "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
]

TIPO_LABEL = {
    "nacional": "Nacional",
    "estadual": "Estadual",
    "municipal": "Municipal",
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
        WHERE feriado_ano = %s AND feriado_tipo = 'nacional'
        ORDER BY feriado_data
    """, (ANO_PRINCIPAL,))

    calendario = gerar_calendario(ANO_PRINCIPAL, feriados_nacionais)

    return render_template("index.html", estados=estados, ano=ANO_PRINCIPAL, calendario=calendario)


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
        WHERE feriado_ano = %s AND (feriado_tipo = 'nacional' OR (feriado_tipo = 'estadual' AND feriado_uf = %s))
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


if __name__ == "__main__":
    app.run(debug=True, port=5001)
