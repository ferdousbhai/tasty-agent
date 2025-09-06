import os
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerStdio
from dotenv import load_dotenv

load_dotenv()

server = MCPServerStdio(  
    'uv', args=['run', 'tasty-agent', 'stdio'], timeout=60, env=os.environ
)
agent = Agent('openai:gpt-4o', toolsets=[server])


async def main():
    async with agent:
        history = None
        print("Tasty Agent Chat (type 'quit' to exit)")
        
        while True:
            try:
                user_input = input("\n👤 ").strip()
                if user_input.lower() in ['quit', 'exit', 'q']:
                    break
                if not user_input:
                    continue
                
                result = await agent.run(user_input, message_history=history)
                print(f"🤖 {result.output}")
                history = result.new_messages()
                
            except (KeyboardInterrupt, EOFError):
                break
            except Exception as e:
                print(f"❌ {e}")

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())