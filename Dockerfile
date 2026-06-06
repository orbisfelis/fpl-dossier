FROM python:3.12-slim

# sqlite3 for the `fpl shell` subcommand; tini for clean signal handling.
RUN apt-get update \
    && apt-get install -y --no-install-recommends sqlite3 tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /app/src/

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    FPL_DB=/data/fpl.db

# Non-root. UID 1000 matches most host users so the mounted /data
# stays writable from the host without chowning.
RUN groupadd -g 1000 fpl && useradd -m -u 1000 -g 1000 fpl \
    && mkdir -p /data && chown -R fpl:fpl /data

USER fpl

VOLUME ["/data"]

ENTRYPOINT ["tini", "--", "python", "-m", "fpl"]
CMD ["--help"]
