# Orchestrator sandbox image (Docker runtime + OpenShell share it).
# Swap SANDBOX_IMAGE / OPENSHELL_IMAGE freely; the entrypoint writes
# /etc/orchestrator/base-commands once per container, and the host caches that list
# per image — no per-project rediscovery.
#
#   docker build -f docker/Dockerfile -t orchestrator-sandbox:local .
FROM python:3.12-slim

# Official Astral uv / uvx (the Python counterpart of npm / npx for MCP packs).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash ca-certificates curl unzip git nodejs npm \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/orchestrator-mcp-client \
    && /opt/orchestrator-mcp-client/bin/pip install --no-cache-dir 'mcp>=1.10,<3' \
    && mkdir -p /etc/orchestrator /opt/orchestrator-mcp /workspace

# In-sandbox MCP supervisor (official mcp SDK client); launched by the host runtime.
COPY src/orchestrator/mcp/supervisor_runtime.py /opt/orchestrator-mcp/supervisor.py
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh /opt/orchestrator-mcp/supervisor.py

WORKDIR /workspace
ENTRYPOINT ["/entrypoint.sh"]
CMD ["sleep", "infinity"]
