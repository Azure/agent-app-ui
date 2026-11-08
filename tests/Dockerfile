ARG BASE_IMAGE
FROM ${BASE_IMAGE}

COPY requirements-test.txt /tmp/requirements-test.txt
COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt -r /tmp/requirements-test.txt \
    && python -m pip check \
    && python -m playwright install --with-deps chromium

COPY --from=orchestrator requirements.txt requirements-hosted.txt /tmp/orchestrator/
COPY tests/requirements-orchestrator.txt /tmp/requirements-orchestrator.txt
RUN python -m venv /opt/orchestrator-test-env \
    && /opt/orchestrator-test-env/bin/python -m pip install --no-cache-dir \
        -r /tmp/orchestrator/requirements-hosted.txt -r /tmp/requirements-orchestrator.txt \
    && /opt/orchestrator-test-env/bin/python -m pip check
