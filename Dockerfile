# ResearchLens AI - production image.  Build:  docker build -t researchlens .
FROM python:3.11-slim

ARG APP_COMMIT=""
ENV APP_COMMIT=${APP_COMMIT} PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    PORT=8000 DB_PATH=/data/researchlens.db

WORKDIR /app

# Optional heavy ML packages (sentence-transformers, torch, numpy pin) are skipped: the app falls
# back to TF-IDF similarity automatically. Remove the grep filter to include them (~2 GB image).
COPY requirements.txt .
RUN grep -viE '^(sentence-transformers|torch|numpy)==' requirements.txt > requirements.docker.txt \
 && pip install -r requirements.docker.txt \
 && rm requirements.docker.txt

COPY backend ./backend
COPY frontend ./frontend
COPY VERSION ./VERSION

RUN useradd --create-home --uid 1000 app \
 && mkdir -p /data /app/uploads /app/reports \
 && chown -R app:app /data /app
USER app
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8000'), timeout=4)" || exit 1

CMD ["sh", "-c", "exec gunicorn --chdir backend app:app --bind 0.0.0.0:${PORT} --workers 2 --threads 4 --timeout 300 --access-logfile -"]
