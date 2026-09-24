# syntax=docker/dockerfile:1
#
# Score capability agent: A2A server where a Claude agent crawls Terraform/Score repos in Git.
# Needs only git + Python; no Terraform, kubectl or cloud credentials. Runtime needs ANTHROPIC_API_KEY.
#
# Build from this directory:
#   podman build --cgroup-manager=cgroupfs -t docker.io/abdurrahman126/score-capability-agent:0.1.0 .

FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 # python-hcl2 caches its grammar next to its own code on first parse; build it now, because
 # at runtime site-packages is read-only and the process is not root.
 && python -c "import hcl2; hcl2.loads('x = 1')"

COPY *.py ./

# Non-root; git and the checkout cache only need /tmp (mounted as an emptyDir in Kubernetes).
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin agent
USER 10001
ENV HOME=/tmp \
    CHECKOUT_DIR=/tmp/capabilities \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

EXPOSE 8080
CMD ["python", "a2a_server.py"]
