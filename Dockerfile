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
RUN pip install --no-cache-dir \
    "fastapi>=0.111" \
    "uvicorn[standard]>=0.29" \
    "sqlalchemy>=2.0" \
    "aiosqlite>=0.20" \
    "httpx>=0.27" \
    "tenacity>=8.2" \
    "pydantic-settings>=2.0" \
    "python-dotenv>=1.0" \
    "aiolimiter>=1.1" \
    "click>=8.1" \
    "rich>=13.0" \
    "rapidfuzz>=3.0" \
    "anthropic>=0.40" \
    "reportlab>=4.0" \
    "fpdf2>=2.7" \
    "beautifulsoup4>=4.12" \
    "yfinance>=0.2" \
    "xlrd>=2.0"

# DB lives in /data volume — create empty placeholder if not mounted
RUN mkdir -p /data
ENV DATABASE_PATH=/data/companies.db

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
