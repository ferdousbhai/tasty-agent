import asyncio
import logging

from agent import create_tastytrader_agent

logger = logging.getLogger(__name__)

async def main():
    try:
        agent = create_tastytrader_agent()
        logger.info("Chat session started")
    except Exception as e:
        logger.error(f"Failed to create agent for chat session: {e}")
        print(f"❌ Failed to start chat: {e}")
        return

    async with agent:
        print("Tasty Agent Chat (type 'quit' to exit)")
        result = None
        while True:
            try:
                user_input = input("\n👤: ").strip()
                if user_input.lower() in ['quit', 'exit', 'q']:
                    logger.info("Chat session ended by user")
                    break
                if not user_input:
                    continue

                logger.debug(f"Processing user input: {user_input}")
                result = await agent.run(user_input, message_history=result.new_messages() if result else None)
                print(f"🤖: {result.output}")

            except (KeyboardInterrupt, EOFError):
                logger.info("Chat session interrupted by user")
                break
            except Exception as e:
                print(f"❌ {e}")
                continue


if __name__ == "__main__":
    asyncio.run(main())
