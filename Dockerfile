# Servidor do painel central (llm-sessoes central). Só stdlib: nada para instalar via pip.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY bin/ bin/
COPY src/ src/

RUN ln -s /app/bin/llm-sessoes /usr/local/bin/llm-sessoes \
 && useradd --system --uid 10001 --user-group --home-dir /data --no-create-home sessoes \
 && mkdir /data && chown sessoes:sessoes /data

USER sessoes
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"]

CMD ["llm-sessoes", "central"]
