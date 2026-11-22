FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Files are listed one by one, not 'COPY . .' -- a new module MUST be added
# here (the portal's own restart loop over a missing twin_view.py, CURRENT_
# STATE 132, is the reason this comment exists). relay.py is the same image
# run with a different command by the 'relay' service (oaap.data.twin 0.3).
# timeseries.py is the time-series driver (RFC-0086), ts_admin.py the
# host's door into it (enable, the minutely job, show). fleet.py is the
# hub's fleet service (RFC-0085 stage 2), run by the 'fleet' service.
# twin_read.py the reads of the door /twin/v1 (RFC-0120 stage 1),
# twin_values.py the value checks and periods (RFC-0120 stage 2).
# twin_write.py holds the write rules the API and the fleet share,
# semantic.py the one setting for OAAP's own semantic IDs; fleet_model,
# fleet_twin and fleet_sink are the fleet's writer into the twin (RFC-0085
# amendment A2); fleet_affected the backward walk from a broken link
# (RFC-0087 §6), used by the fleet service and by the read in app.py.
# assets_model, assets_twin and assets_run are the tenant reader (RFC-0088):
# the host runs assets_run.py here every five minutes (docker exec), and
# app.py's /internal/twin/assets reads what it wrote.
COPY app.py relay.py timeseries.py ts_admin.py fleet.py \
     twin_write.py twin_read.py twin_values.py semantic.py fleet_model.py fleet_twin.py fleet_sink.py \
     fleet_affected.py assets_model.py assets_twin.py assets_run.py mcp_door.py ./
EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "app:app"]
