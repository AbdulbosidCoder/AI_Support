FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY ai_support ai_support
COPY data/knowledge_base data/knowledge_base

RUN useradd --create-home --uid 10001 bot
USER bot

CMD ["python", "-m", "ai_support"]
