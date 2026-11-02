# --- Etape 1 : build de l'interface React ---
FROM node:20-slim AS frontend
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- Etape 2 : API Python qui sert aussi l'interface ---
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# torch CPU d'abord : evite de tirer les ~2 Go de CUDA via sentence-transformers.
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu
COPY requirements.txt .
RUN pip install -r requirements.txt

# Modeles dans l'image : en cloud (scale-to-zero, disque ephemere) rien a retelecharger au reveil.
RUN python -c "from sentence_transformers import CrossEncoder, SentenceTransformer; SentenceTransformer('intfloat/multilingual-e5-small'); CrossEncoder('BAAI/bge-reranker-base')"

COPY app ./app
COPY --from=frontend /frontend/dist ./frontend/dist
EXPOSE 8000
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
