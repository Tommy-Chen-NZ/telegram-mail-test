FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY mailagent.py gmail_api.py webhook.py env_config.py /app/
COPY compose.yaml prepare.sh mailagent.py gmail_api.py webhook.py env_config.py requirements.txt /opt/deployment/
COPY scripts/deploy-server.sh scripts/pull-release.sh /opt/deployment/scripts/
COPY compose.ngrok.yaml ngrok_setup.py /opt/deployment/
# Older pull scripts copy these names. Bundle inert notices, not a UI server.
RUN printf '%s\n' 'raise SystemExit("Dashboard removed. Run python3 mailagent.py status.")' > /opt/deployment/dashboard.py \
    && cp /opt/deployment/dashboard.py /opt/deployment/demo_dashboard.py
ENTRYPOINT ["python", "/app/mailagent.py"]
CMD ["run"]
