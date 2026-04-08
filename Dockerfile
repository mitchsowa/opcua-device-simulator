FROM python:3.11-slim

LABEL maintainer="mitchsowa"
LABEL description="OPC-UA simulator — Opto22 groov RIO (CODESYS 3.5), Siemens S7-1200, Unitronics PLC"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY opcua_sim.py .

# OPC-UA default port
EXPOSE 4840

ENTRYPOINT ["python", "opcua_sim.py"]

# Defaults — override at runtime:
#   docker run -p 4840:4840 opcua-sim --interval 0.5
CMD ["--host", "0.0.0.0", "--port", "4840", "--interval", "1.0"]
