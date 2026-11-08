ARG BASE_IMAGE
FROM ${BASE_IMAGE}

COPY requirements-test.txt /tmp/requirements-test.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements-test.txt \
    && python -m playwright install --with-deps chromium
