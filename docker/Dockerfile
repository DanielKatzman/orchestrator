# syntax=docker/dockerfile:1.7
ARG PYTHON312=python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
ARG PYTHON314=python:3.14.8-slim-bookworm@sha256:44e000a07b48bef1517a2b14adc93c17af728ca98858b5e24c6e1b18de1b2ef2
ARG NODE_IMAGE=node:24.13.0-bookworm-slim@sha256:4660b1ca8b28d6d1906fd644abe34b2ed81d15434d26d845ef0aced307cf4b6f

FROM ${PYTHON314} AS sources
ARG COMPONENT_LOCK=composition/pipeline.lock.json
WORKDIR /build
COPY ${COMPONENT_LOCK} /build/pipeline.lock.json
COPY tools/fetch_components.py /build/fetch_components.py
RUN python fetch_components.py --lock pipeline.lock.json --output /opt/components

FROM ${PYTHON314} AS stage12
# Relocate the full CPython installation before creating the venv. Both trees
# keep these final paths; the 3.12 interpreter remains at /usr/local in runtime.
RUN cp -a /usr/local /opt/python314
ENV LD_LIBRARY_PATH=/opt/python314/lib PYTHONDONTWRITEBYTECODE=1
RUN /opt/python314/bin/python3.14 -m venv /opt/venvs/stage12
COPY --from=sources /opt/components/stage1 /opt/components/stage1
COPY --from=sources /opt/components/stage2 /opt/components/stage2
COPY composition/runtime/stage12.txt /opt/runtime-stage12.txt
RUN --mount=type=cache,target=/root/.cache/pip /opt/venvs/stage12/bin/python -m pip install \
    --report /opt/stage12-install.json -c /opt/runtime-stage12.txt -c /opt/components/stage1/constraints.txt '/opt/components/stage1[runtime]' '/opt/components/stage2[inference]'
RUN /opt/venvs/stage12/bin/python -m universal_segmenter.release_cli verify --models /opt/components/stage1/models && \
    /opt/venvs/stage12/bin/python -m paper_reconstruction_stage2.release_cli verify --models /opt/components/stage2/models && \
    /opt/venvs/stage12/bin/python -m pip freeze > /opt/stage12-requirements.txt

FROM ${PYTHON312} AS stage34
RUN python -m venv /opt/venvs/stage3 && python -m venv /opt/venvs/stage4
COPY --from=sources /opt/components/stage3 /opt/components/stage3
COPY --from=sources /opt/components/stage4 /opt/components/stage4
COPY composition/runtime/stage3.txt /opt/runtime-stage3.txt
COPY composition/runtime/stage4.txt /opt/runtime-stage4.txt
RUN --mount=type=cache,target=/root/.cache/pip /opt/venvs/stage3/bin/python -m pip install --report /opt/torch-install.json -c /opt/runtime-stage3.txt torch==2.7.1+cpu --index-url https://download.pytorch.org/whl/cpu
RUN --mount=type=cache,target=/root/.cache/pip /opt/venvs/stage3/bin/python -m pip install \
    --report /opt/stage3-install.json -c /opt/runtime-stage3.txt -c /opt/components/stage3/constraints.txt /opt/components/stage3/contracts/stage3_to_stage4/v2/sdk '/opt/components/stage3[inference,test]' && \
    /opt/venvs/stage4/bin/python -m pip install --report /opt/stage4-install.json -c /opt/runtime-stage4.txt -c /opt/components/stage4/stages/stage4/constraints.txt \
    /opt/components/stage3/contracts/stage3_to_stage4/v2/sdk /opt/components/stage4/stages/stage4
RUN /opt/venvs/stage3/bin/python -m paper_reconstruction_stage3_release verify --models /opt/components/stage3/models && \
    /opt/venvs/stage3/bin/python -m pip freeze > /opt/stage3-requirements.txt && \
    /opt/venvs/stage4/bin/python -m pip freeze > /opt/stage4-requirements.txt

FROM ${NODE_IMAGE} AS web
WORKDIR /web
COPY web/package*.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY web/ ./
RUN npm run build

FROM ${PYTHON312} AS appdeps
RUN python -m venv /opt/venvs/app
COPY requirements.txt /requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip /opt/venvs/app/bin/python -m pip install --report /opt/app-install.json -r /requirements.txt

FROM ${PYTHON312} AS runtime
LABEL org.opencontainers.image.source="https://github.com/DanielKatzman/orchestrator" \
      org.opencontainers.image.description="Self-contained four-stage CPU paper reconstruction studio"
RUN apt-get update && apt-get install -y --no-install-recommends tini libgomp1 && \
    rm -rf /var/lib/apt/lists/* && useradd --uid 10001 --create-home paper && \
    mkdir /data && chown paper:paper /data
COPY --from=stage12 /opt/python314 /opt/python314
COPY --from=stage12 /opt/venvs/stage12 /opt/venvs/stage12
COPY --from=stage12 /opt/stage12-requirements.txt /opt/build/stage12-requirements.txt
COPY --from=stage12 /opt/stage12-install.json /opt/build/stage12-install.json
COPY --from=stage34 /opt/venvs/stage3 /opt/venvs/stage3
COPY --from=stage34 /opt/venvs/stage4 /opt/venvs/stage4
COPY --from=stage34 /opt/stage3-requirements.txt /opt/build/stage3-requirements.txt
COPY --from=stage34 /opt/stage4-requirements.txt /opt/build/stage4-requirements.txt
COPY --from=stage34 /opt/*-install.json /opt/build/
COPY --from=appdeps /opt/venvs/app /opt/venvs/app
COPY --from=appdeps /opt/app-install.json /opt/build/app-install.json
COPY --from=sources /opt/components/stage1/models /opt/components/stage1/models
COPY --from=sources /opt/components/stage2/models /opt/components/stage2/models
COPY --from=sources /opt/components/stage3/models /opt/components/stage3/models
COPY --from=sources /opt/components/stage4/stages/stage4/configs /opt/components/stage4/stages/stage4/configs
COPY --from=sources /opt/components/stage1/docs/THIRD_PARTY_LICENSES.md /opt/notices/stage1.md
COPY --from=sources /opt/components/stage2/LICENSE /opt/notices/stage2-LICENSE
COPY --from=sources /opt/components/stage4/LICENSE /opt/notices/stage4-LICENSE
WORKDIR /app
ENV LD_LIBRARY_PATH=/opt/python314/lib PYTHONUTF8=1 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PAPER_DATA=/data PAPER_COMPONENTS=/opt/components OMP_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4
COPY app/ /app/app/
COPY tools/stage_io.py /app/tools/stage_io.py
COPY tools/build_identity.py /app/tools/build_identity.py
COPY requirements.txt /app/requirements.txt
COPY composition/ /app/composition/
COPY --from=sources /build/pipeline.lock.json /app/composition/pipeline.lock.json
COPY tests/fixtures/ /app/tests/fixtures/
COPY --from=web /web/dist /app/web/dist
RUN /opt/venvs/stage12/bin/python -c 'import onnxruntime,fitz; print(onnxruntime.get_available_providers())' && \
    /opt/venvs/stage3/bin/python -c 'import torch; assert torch.version.cuda is None' && \
    /opt/venvs/stage4/bin/python -c 'import paper_reconstruction_stage4,paper_reconstruction_contracts' && \
    /opt/venvs/app/bin/python -c 'import fastapi' && /opt/venvs/app/bin/python /app/tools/build_identity.py && \
    /opt/venvs/stage4/bin/python -c 'import json; from paper_reconstruction_stage4.outputs import implementation_identity; lock=json.load(open("/app/composition/pipeline.lock.json")); assert implementation_identity()["sha256"] == lock["components"]["stage4"]["implementation_sdk_fingerprint"]'
USER paper
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s CMD /opt/venvs/app/bin/python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz',timeout=3)"
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/venvs/app/bin/python", "-m", "app.supervisor"]
