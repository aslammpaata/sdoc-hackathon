FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Cloud Run injects $PORT at runtime — you MUST listen on it, not a hardcoded port
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}