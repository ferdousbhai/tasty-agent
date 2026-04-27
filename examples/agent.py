import logging
import os

from dotenv import load_dotenv
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerStdio

logger = logging.getLogger(__name__)
load_dotenv()


def create_tastytrader_agent() -> Agent:
    """Create and return a configured agent instance."""
    model_identifier = os.getenv("MODEL_IDENTIFIER", "openai:gpt-5-mini")
    logger.info(f"Creating agent with model: {model_identifier}")

    server = MCPServerStdio("uv", args=["run", "tasty-agent", "stdio"], timeout=60, env=dict(os.environ))
    return Agent(model_identifier, toolsets=[server])
