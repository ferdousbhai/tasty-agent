from tasty_functions import (
    get_price,
    buy_to_open,
    sell_to_close,
    analyze_ticker,
    is_market_open,
    get_balances,
    get_positions,
)

from marvin.beta import Assistant
from marvin.beta.assistants import pprint_messages

from tastytrade.utils import now_in_new_york


assistant = Assistant(
    name="Tasty App",
    instructions="""You are a trading assistant with access to tools that allow you to interact with TastyTrade.
    
    When listing option positions, please use the following format:
    TICKER STRIKEprice(c or p) date
    Here's are a few examples:
    SPY 25p 3/27
    AAPL 500c 4/17
    
    When placing orders, please check first if the market is open, and if not, wait until it does before placing your order.
    """,
    tools=[
        get_price,
        get_balances,
        get_positions,
        analyze_ticker,
        is_market_open,
        wait_until_market_open,
        buy_to_open,
        sell_to_close,
    ],
)


user_input = ""
while user_input.lower() != "exit":
    user_input = input("Enter your message (type 'exit' to quit): ")
    if user_input.lower() != "exit":
        assistant.say(f"{user_input} \n\nCurrent time in NYC: {now_in_new_york()}")
        pprint_messages(assistant.default_thread.get_messages())
