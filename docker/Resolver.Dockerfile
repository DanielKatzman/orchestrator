FROM python:3.14.8-slim-bookworm@sha256:44e000a07b48bef1517a2b14adc93c17af728ca98858b5e24c6e1b18de1b2ef2
RUN pip install --no-cache-dir PyYAML==6.0.3
COPY tools/resolve.py /tool/resolve.py
WORKDIR /workspace
ENTRYPOINT ["python", "/tool/resolve.py"]
