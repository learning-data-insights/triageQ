# triageQ hosted web build. See DEPLOY.md.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRIAGEQ_DATA_DIR=/data

WORKDIR /app
COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt

COPY *.py ./
COPY assets ./assets
COPY criteria-profiles ./criteria-profiles
COPY .streamlit ./.streamlit

RUN useradd --create-home --uid 1000 triageq \
 && mkdir -p /data && chown triageq:triageq /data
USER triageq

EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')"
CMD ["streamlit", "run", "web_app.py", "--server.address=0.0.0.0", "--server.port=8501"]
