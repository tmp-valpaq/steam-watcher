FROM python:3.11-slim

ARG INSTALL_DOTABUFF_BROWSER=0
ARG INSTALL_LOCAL_CHROMIUM=0

WORKDIR /app

COPY requirements.txt requirements-browser.txt ./

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_DOTABUFF_BROWSER" = "1" ]; then pip install --no-cache-dir -r requirements-browser.txt; fi

# --only-shell downloads just chrome-headless-shell (what headless=True runs
# anyway) instead of full Chromium; --with-deps installs the exact OS libs it
# needs, replacing the old hand-maintained list (which dragged in gtk3 etc).
RUN if [ "$INSTALL_LOCAL_CHROMIUM" = "1" ]; then \
        python -m playwright install --with-deps --only-shell chromium \
        && rm -rf /var/lib/apt/lists/*; \
    fi

COPY src/ ./src/
COPY schema.sql .

RUN mkdir -p /app/data /app/.cache/dotabuff-playwright-profile /app/.cache/dotabuff-playwright-output

CMD ["python", "-m", "src.main"]
