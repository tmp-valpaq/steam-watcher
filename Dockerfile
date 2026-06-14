FROM python:3.11-slim

ARG INSTALL_DOTABUFF_BROWSER=0
ARG INSTALL_LOCAL_CHROMIUM=0

WORKDIR /app

COPY requirements.txt requirements-browser.txt ./

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_DOTABUFF_BROWSER" = "1" ]; then pip install --no-cache-dir -r requirements-browser.txt; fi

RUN if [ "$INSTALL_LOCAL_CHROMIUM" = "1" ]; then \
        apt-get update && apt-get install -y --no-install-recommends \
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
        && rm -rf /var/lib/apt/lists/* \
        && python -m playwright install chromium; \
    fi

COPY src/ ./src/
COPY schema.sql .

RUN mkdir -p /app/data /app/.cache/dotabuff-playwright-profile /app/.cache/dotabuff-playwright-output

CMD ["python", "-m", "src.main"]
