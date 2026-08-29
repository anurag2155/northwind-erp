# Standard library only, so there is nothing to install and nothing to pin.
FROM python:3.12-slim
WORKDIR /app
COPY erp.py server.py readmodel.py regression.py proofs.py ./
# Seeded on boot by server.py. ERP_DEMO makes the six role tokens predictable
# (demo-ravi, demo-kabir, ...) so a reviewer can sign in without a side channel.
ENV PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8080 ERP_DEMO=1
EXPOSE 8080
# No shell form: signals reach the process, so the container stops cleanly.
CMD ["python3", "server.py"]
