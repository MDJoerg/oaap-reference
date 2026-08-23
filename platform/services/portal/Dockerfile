FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py store_view.py instance_view.py fleet_view.py ./
EXPOSE 8000
# --timeout above the deploy hook's synchronous wait (120s), so a
# worker waiting for a build result is not killed mid-request
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "4", "--timeout", "150", "app:app"]
