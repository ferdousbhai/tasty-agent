import asyncio
import logging

from examples.agent import create_tastytrader_agent

logger = logging.getLogger(__name__)

EXIT_COMMANDS = {"quit", "exit", "q"}


async def main() -> None:
    agent = create_tastytrader_agent()

    async with agent:
        print("Tasty Agent Chat (type 'quit' to exit)")
        message_history = None
        while True:
            try:
                user_input = input("\n👤: ").strip()
                if user_input.lower() in EXIT_COMMANDS:
                    break
                if not user_input:
                    continue

                result = await agent.run(user_input, message_history=message_history)
                message_history = result.new_messages()
                print(f"🤖: {result.output}")

            except (KeyboardInterrupt, EOFError):
                break
            except Exception as e:
                print(f"❌ {e}")


if __name__ == "__main__":
    asyncio.run(main())
