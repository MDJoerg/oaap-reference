FROM python:3.12-slim
WORKDIR /app
# Build context is services/, not services/portal -- see
# docker-compose.yml. place.py is shared with identity on purpose, and
# every local import of app.py must appear below or the container dies
# in a restart loop with ModuleNotFoundError (CURRENT_STATE 132).
COPY portal/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY place.py .
COPY idp.py .
COPY portal/app.py portal/store_view.py portal/instance_view.py portal/fleet_view.py portal/deploy_state.py portal/twin_view.py portal/relay_view.py portal/diagnose_view.py ./
EXPOSE 8000
# --timeout above the deploy hook's synchronous wait (120s), so a
# worker waiting for a build result is not killed mid-request
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "4", "--timeout", "150", "app:app"]
