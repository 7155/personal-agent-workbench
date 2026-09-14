# Build with a reviewed Pi payload supplied as the `pi_payload` build context.
# Example: docker buildx build --build-context pi_payload=/path/to/payload \
#   -f deploy/team/worker.Dockerfile -t paw-team-worker:local .
FROM node:22-bookworm-slim AS node
FROM python:3.12-slim-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
RUN apt-get update && apt-get install -y --no-install-recommends git ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir 'pypdf>=5,<7' 'PyYAML>=6,<7'
WORKDIR /opt/paw
COPY rag_ime /opt/paw/rag_ime
COPY --from=pi_payload / /opt/pi/
# The reviewed payload is immutable in the worker.  Preserve its content
# while granting the configured non-root worker UID read/execute traversal.
RUN chmod -R a+rX /opt/pi
ENV PYTHONPATH=/opt/paw PYTHONDONTWRITEBYTECODE=1 RAG_IME_TEAM_WORKER=1
USER 65532:65532
WORKDIR /workspace
# The trusted server passes the command. There is no network by default, and
# only the launcher can attach an attempt's Unix broker and workspace mounts.
CMD ["python3", "-m", "rag_ime.team.worker_proxy", "--", "node", "/opt/pi/runtime-host/cli.mjs"]
