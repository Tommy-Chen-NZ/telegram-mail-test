FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY mailagent.py dashboard.py demo_dashboard.py webhook.py /app/
COPY frontend/dist /app/frontend/dist
ENTRYPOINT ["python", "/app/mailagent.py"]
CMD ["run"]
