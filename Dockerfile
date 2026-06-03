FROM python:3.11-slim

WORKDIR /app

# Install OS/runtime deps for Playwright Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libglib2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install chromium

# Copy source code
COPY src/ ./src/
COPY schema.sql .

# Create data/cache directories for SQLite + browser profile/output
RUN mkdir -p /app/data /app/.cache/dotabuff-playwright-profile /app/.cache/dotabuff-playwright-output

CMD ["python", "-m", "src.main"]
