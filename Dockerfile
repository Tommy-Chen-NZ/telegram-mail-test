FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY mailagent.py dashboard.py demo_dashboard.py webhook.py /app/
COPY frontend/dist /app/frontend/dist
COPY compose.yaml prepare.sh mailagent.py dashboard.py demo_dashboard.py webhook.py requirements.txt /opt/deployment/
COPY scripts/deploy-server.sh scripts/pull-release.sh /opt/deployment/scripts/
ENTRYPOINT ["python", "/app/mailagent.py"]
CMD ["run"]
