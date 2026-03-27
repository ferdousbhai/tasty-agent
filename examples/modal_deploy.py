"""Deploy tasty-agent as a remote MCP server on Modal.

Exposes the tasty-agent MCP server over streamable HTTP so it can be
consumed by any MCP client (Claude Desktop, CF Workers, etc.).

Setup:
    1. pip install modal && modal setup

    2. Create a Modal secret named "tasty-agent-secrets":
       modal secret create tasty-agent-secrets \\
         TASTYTRADE_CLIENT_SECRET=your_secret \\
         TASTYTRADE_REFRESH_TOKEN=your_token \\
         TASTYTRADE_ACCOUNT_ID=your_account_id

    3. Create a proxy auth token at https://modal.com/settings/proxy-auth-tokens

    4. Update MODAL_HOST below with your workspace name.

    5. Deploy:
       modal deploy examples/modal_deploy.py

    6. The MCP endpoint will be at:
       https://<workspace>--tasty-agent-mcp-server.modal.run/mcp

       Clients must include Modal-Key and Modal-Secret headers from step 3.

Dev (ephemeral, hot-reload):
    modal serve examples/modal_deploy.py
"""

import modal

app = modal.App("tasty-agent")

image = modal.Image.debian_slim(python_version="3.12").pip_install(
    "tasty-agent>=4.0.0",
)

# Replace with your Modal workspace name
MODAL_HOST = "YOUR_WORKSPACE--tasty-agent-mcp-server.modal.run"


@app.function(
    image=image,
    secrets=[modal.Secret.from_name("tasty-agent-secrets")],
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
