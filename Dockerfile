FROM python:3.11-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl tzdata fonts-dejavu-core && \
    rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app. A glob rather than a list of names: every module added later
# ships automatically, instead of the build succeeding and the container
# then dying on ModuleNotFoundError.
COPY *.py ./
COPY templates/ templates/
COPY static/ static/

# Create dirs and download essential ephemeris files
RUN mkdir -p /app/static /app/ephe /app/data && \
    curl -sL -o /app/ephe/seas_18.se1 https://raw.githubusercontent.com/aloistr/swisseph/master/ephe/seas_18.se1 && \
    curl -sL -o /app/ephe/sepl_18.se1 https://raw.githubusercontent.com/aloistr/swisseph/master/ephe/sepl_18.se1 && \
    curl -sL -o /app/ephe/semo_18.se1 https://raw.githubusercontent.com/aloistr/swisseph/master/ephe/semo_18.se1 && \
    curl -sL -o /app/ephe/sefstars.txt https://raw.githubusercontent.com/aloistr/swisseph/master/ephe/sefstars.txt && \
    curl -sL -o /app/ephe/seorbel.txt https://raw.githubusercontent.com/aloistr/swisseph/master/ephe/seorbel.txt

ENV SE_EPHE_PATH=/app/ephe
ENV HOSTNAME=0.0.0.0

# The SQLite file must outlive the container. Mount a persistent volume here,
# otherwise every deploy starts with an empty database.
VOLUME ["/app/data"]

EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
