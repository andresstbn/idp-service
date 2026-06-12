FROM python:3.12-slim

# Usuario sin privilegios (no root) para Cloud Run.
RUN useradd -m -u 1000 appuser

WORKDIR /app

# Instalar dependencias primero para aprovechar la cache de capas.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chown -R appuser:appuser /app
USER appuser

# Puerto 8080 por defecto de Cloud Run; respetar PORT si se inyecta.
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
