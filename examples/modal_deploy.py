"""Deploy tasty-agent as a remote MCP server on Modal.

Exposes the tasty-agent MCP server over streamable HTTP so it can be
consumed by any MCP client (Claude Desktop, CF Workers, etc.).

Setup:
    1. uvx modal setup

    2. Create a Modal secret named "tasty-agent-secrets":
       uvx modal secret create tasty-agent-secrets \\
         TASTYTRADE_CLIENT_SECRET=your_secret \\
         TASTYTRADE_REFRESH_TOKEN=your_token \\
         TASTYTRADE_ACCOUNT_ID=your_account_id

    3. Create a proxy auth token at https://modal.com/settings/proxy-auth-tokens

    4. Export your Modal host:
       export MODAL_HOST=<workspace>--tasty-agent-mcp-server.modal.run

       Optional overrides:
       export TASTY_AGENT_VERSION=4.1.2
       export TASTY_AGENT_SECRET_NAME=tasty-agent-secrets

    5. Deploy:
       uvx modal deploy examples/modal_deploy.py

    6. The MCP endpoint will be at:
       https://<workspace>--tasty-agent-mcp-server.modal.run/mcp

       Clients must include Modal-Key and Modal-Secret headers from step 3.

Dev (ephemeral, hot-reload):
    uvx modal serve examples/modal_deploy.py
"""

import os

import modal

APP_NAME = os.environ.get("MODAL_APP_NAME", "tasty-agent")
PACKAGE_VERSION = os.environ.get("TASTY_AGENT_VERSION")
SECRET_NAME = os.environ.get("TASTY_AGENT_SECRET_NAME", "tasty-agent-secrets")
MODAL_HOST = os.environ.get("MODAL_HOST", "ai-clone-company--tasty-agent-mcp-server.modal.run")

app = modal.App(APP_NAME)

base_image = modal.Image.debian_slim(python_version="3.12")

if PACKAGE_VERSION:
    image = base_image.pip_install(f"tasty-agent=={PACKAGE_VERSION}")
else:
    image = base_image.pip_install_from_pyproject("pyproject.toml").add_local_python_source("tasty_agent")


@app.function(
    image=image,
    secrets=[modal.Secret.from_name(SECRET_NAME)],
)
@modal.asgi_app(requires_proxy_auth=True)
def mcp_server():
    from mcp.server.fastmcp.server import TransportSecuritySettings

    from tasty_agent.server import mcp_app

    mcp_app.settings.stateless_http = True
    mcp_app.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[MODAL_HOST],
    )

    return mcp_app.streamable_http_app()
