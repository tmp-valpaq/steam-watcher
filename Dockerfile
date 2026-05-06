FROM python:3.11-slim

WORKDIR /app

# Install dependencies first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/ ./src/
COPY schema.sql .

# Create data directory for SQLite
RUN mkdir -p /app/data

CMD ["python", "-m", "src.main"]
