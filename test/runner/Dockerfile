# The reference's test suite runs on Linux, like the platform does:
# several services lock files with fcntl, which Windows does not have,
# and the services pin their libraries in requirements.txt. This image
# is those pins plus what the tests themselves import -- nothing else.
#
#   bash test/runner/run.sh            # every test, summary at the end
#   bash test/runner/run.sh test_x.py  # just these
FROM python:3.12-slim
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       git openssl procps \
    && rm -rf /var/lib/apt/lists/*
COPY requirements/ /tmp/req/
RUN pip install --no-cache-dir $(for f in /tmp/req/*.txt; do echo -r $f; done) \
       "PyYAML==6.0.3" "Jinja2==3.1.*"
# appctl asks `docker inspect` about containers that do not exist in a
# test; on a developer's machine that fails with an exit code, here the
# binary itself would be missing (FileNotFoundError). A stand-in that
# fails the same way -- no test may need a real Docker.
RUN printf '#!/bin/sh\necho "docker: not available in the test container" >&2\nexit 1\n' \
       > /usr/local/bin/docker && chmod 0755 /usr/local/bin/docker
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
