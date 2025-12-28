---
description: Triggers on TastyTrade account queries, trading, positions, orders, options analysis.
---

# TastyTrade Trading

The **tasty-agent** MCP server connects to TastyTrade brokerage. Tools are self-documenting.

## Guidelines

- Check market_status before placing orders
- Verify positions before suggesting trades
- Options: always check Greeks (get_greeks) for risk assessment
- Order placement requires explicit user confirmation
