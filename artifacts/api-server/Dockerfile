FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    AEREO_DATABASE_URL=sqlite:////app/data/aereo.sqlite3

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY geospatial_api ./geospatial_api

RUN mkdir -p /app/data
EXPOSE 8080
CMD ["sh", "-c", "python -m uvicorn geospatial_api.main:app --host 0.0.0.0 --port ${PORT}"]
