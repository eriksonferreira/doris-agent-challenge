FROM python:3.11-slim
WORKDIR /eval
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY checks.py runner.py scenarios.yaml ./
ENTRYPOINT ["python", "runner.py"]
CMD ["--scenarios", "scenarios.yaml", "--out", "/out"]
