FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir requests
COPY cleaner.py .
CMD ["python", "-u", "cleaner.py"]
