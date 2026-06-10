FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY altura_monitor.py .

# El estado se persiste aqui; monta un volumen para que sobreviva reinicios.
VOLUME ["/data"]
ENV STATE_FILE=/data/altura_monitor_state.json

CMD ["python", "altura_monitor.py"]
