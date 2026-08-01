FROM python:3.11-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl tzdata && \
    rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY app.py .
COPY chart_svg.py .
COPY translations.py .
COPY numerology.py .
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

EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
