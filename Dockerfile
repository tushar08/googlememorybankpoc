# Optional: run the gateway on Cloud Run (the slide lists production deployment as "proposed").
#   gcloud run deploy cx-memory-gateway --source . --region us-central1 \
#     --env-vars-file env.yaml --service-account <sa-with-aiplatform.user> --no-allow-unauthenticated
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY cx_memory ./cx_memory
COPY data ./data
ENV PORT=8080 BACKEND=vertex
CMD exec uvicorn cx_memory.api:app --host 0.0.0.0 --port ${PORT}
