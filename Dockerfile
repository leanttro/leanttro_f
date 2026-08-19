# ════════════════════════════════════════════════════════════
#  Dockerfile — feriados2027.com.br
#  App Flask independente, roda com Gunicorn em produção.
# ════════════════════════════════════════════════════════════

FROM python:3.12-slim

WORKDIR /app

# Dependências de sistema pro psycopg2 compilar (se não usar o -binary)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=5001
EXPOSE 5001

# 3 workers é um ponto de partida razoável pra uma VPS pequena;
# ajuste conforme os recursos do servidor.
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers 3 --timeout 60 app:app"]
