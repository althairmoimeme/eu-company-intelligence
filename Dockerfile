FROM python:3.11-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && rm -rf /var/lib/apt/lists/*

# Copy source
COPY pyproject.toml .
COPY api/ ./api/
COPY scraper/ ./scraper/
COPY frontend/ ./frontend/
COPY run.py .

# Install Python deps
RUN pip install --no-cache-dir -e "."

# DB lives in /data volume — create empty placeholder if not mounted
RUN mkdir -p /data
ENV DATABASE_PATH=/data/companies.db

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
