import logging
import os

from dotenv import load_dotenv
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerStdio

logger = logging.getLogger(__name__)
load_dotenv()

def create_tastytrader_agent() -> Agent:
    """Create and return a configured agent instance."""

    model_identifier = os.getenv('MODEL_IDENTIFIER', 'openai:gpt-4.1')
    if not model_identifier:
        logger.error("MODEL_IDENTIFIER environment variable is required")
        raise ValueError("MODEL_IDENTIFIER environment variable is required")

    logger.info(f"Creating agent with model: {model_identifier}")

    try:
        server = MCPServerStdio(
            'uv', args=['run', 'tasty-agent', 'stdio'], timeout=60, env=dict(os.environ)
        )
        agent = Agent(model_identifier, toolsets=[server])
        agent.set_mcp_sampling_model() # Allows MCP server to make LLM calls via the MCP client

        logger.info("TastyTrader agent created successfully")
        return agent
    except Exception as e:
        logger.error(f"Failed to create TastyTrader agent: {e}")
        raise