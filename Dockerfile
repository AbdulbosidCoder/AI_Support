FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ai_support ai_support
COPY data/knowledge_base data/knowledge_base
CMD ["python", "-m", "ai_support"]
