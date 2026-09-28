FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY antal_sheets ./antal_sheets
EXPOSE 8080
CMD ["python", "-m", "antal_sheets", "--config", "/app/config/config.yaml"]
