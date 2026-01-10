# Security Audit Report: tasty-agent MCP Server

**Audit Date:** 2026-01-10
**Repository:** ferdousbhai/tasty-agent
**Auditor:** Claude (Automated Security Review)
**Overall Risk Assessment:** LOW

---

## Executive Summary

This audit examined the tasty-agent MCP server for security concerns before connecting it to a brokerage account. The codebase is **relatively safe** with no malicious code detected. Key findings:

- Credentials are handled securely via environment variables only
- All network traffic goes exclusively to TastyTrade and DXFeed (market data) endpoints
- No telemetry, analytics, or data exfiltration detected
- Read-only mode is achievable by removing 4 functions
- Dependencies are legitimate and properly pinned

---

## 1. Credential Handling

**Risk Level:** LOW

### How Credentials Are Used

| Credential | Location | Purpose |
|------------|----------|---------|
| `TASTYTRADE_CLIENT_SECRET` | `server.py:109`, `background.py:26` | OAuth client secret for API authentication |
| `TASTYTRADE_REFRESH_TOKEN` | `server.py:110`, `background.py:27` | OAuth refresh token for session creation |
| `TASTYTRADE_ACCOUNT_ID` | `server.py:111` | Account selector (optional, defaults to first account) |

### Security Analysis

**Credentials are read from environment variables only:**
```python
# server.py:109-111
client_secret = os.getenv("TASTYTRADE_CLIENT_SECRET")
refresh_token = os.getenv("TASTYTRADE_REFRESH_TOKEN")
account_id = os.getenv("TASTYTRADE_ACCOUNT_ID")
```

**Are credentials logged?**
- Credentials themselves are **never logged**
- Only generic error messages are logged (e.g., "Missing Tastytrade OAuth credentials")
- Account numbers appear in logs (`server.py:123,131,133,136`) but not secrets

**Are credentials written to disk?**
- **NO** - No file writes detected in the codebase
- No `.env` file creation or credential caching

**Are credentials transmitted to non-TastyTrade endpoints?**
- **NO** - Credentials are only passed to the `tastytrade.Session()` constructor
- The tastytrade SDK handles all API communication

### Recommendation
PASS - Credential handling follows security best practices.

---

## 2. Network Calls

**Risk Level:** LOW

### All Outbound Network Requests

The codebase makes **no direct HTTP requests**. All network communication is delegated to the `tastytrade` SDK.

| Component | Destination | Purpose | Source |
|-----------|-------------|---------|--------|
| `tastytrade.Session` | `api.tastytrade.com` | Authentication, account data, orders | SDK |
| `tastytrade.DXLinkStreamer` | DXFeed servers | Real-time market data streaming | SDK |
| `tastytrade` API calls | `api.tastytrade.com` | All trading operations | SDK |

### URL Analysis

Searched for all URLs in the codebase:

**Legitimate URLs found:**
- `https://my.tastytrade.com/` - OAuth setup instructions (README only)
- PyPI package URLs in `uv.lock` (dependency resolution)
- Marketing/documentation URLs in `.md` files (not code)

**Suspicious URLs found:**
- **NONE**

### Direct HTTP Client Usage

```bash
# Search results for HTTP clients in source code:
Grep: requests\.|httpx\.|urllib|aiohttp\.ClientSession
Result: No matches in Python source files
```

The `httpx` and `aiohttp` packages appear in dependencies but are used by the `tastytrade` SDK internally, not by this codebase directly.

### Recommendation
PASS - All network traffic is confined to TastyTrade's ecosystem.

---

## 3. Read-Only Feasibility

**Risk Level:** N/A (Configuration Question)

### Can You Use Read-Only Mode?

**YES** - You can disable write operations without breaking the server.

### Write Operations (Functions to Remove)

| Function | Location | Purpose |
|----------|----------|---------|
| `place_order` | `server.py:548-621` | Place new orders |
| `replace_order` | `server.py:625-666` | Modify existing orders |
| `delete_order` | `server.py:670-675` | Cancel orders |
| `manage_private_watchlist` | `server.py:703-773` | Add/remove watchlist symbols |
| `delete_private_watchlist` | `server.py:777-780` | Delete watchlists |

### How to Disable Write Operations

**Option 1: Remove the decorators** (simplest)
Comment out or remove the `@mcp_app.tool()` decorator above each write function:
```python
# @mcp_app.tool()  # Disabled for read-only mode
async def place_order(...):
    ...
```

**Option 2: Delete the functions entirely**
Remove lines 548-780 from `server.py`. The server will still function with all read operations.

**Option 3: Create a read-only fork**
Fork the repository and maintain a read-only version.

### Functions That Remain Safe (Read-Only)

- `get_balances` - Account balances
- `get_positions` - Current positions
- `get_net_liquidating_value_history` - Portfolio history
- `get_transaction_history` - Past transactions
- `get_order_history` - Past orders
- `get_quotes` - Real-time quotes
- `get_greeks` - Options Greeks
- `get_market_metrics` - IV rank, beta, etc.
- `market_status` - Market hours
- `search_symbols` - Symbol search
- `get_current_time_nyc` - Current time
- `get_live_orders` - View pending orders
- `get_watchlists` - View watchlists

### Recommendation
Removing write operations is straightforward and safe.

---

## 4. Dependencies

**Risk Level:** LOW

### Production Dependencies (pyproject.toml:7-14)

| Package | Version | Purpose | Verdict |
|---------|---------|---------|---------|
| `mcp[cli]` | >=1.14.0 | Model Context Protocol SDK | LEGITIMATE - Anthropic's official MCP library |
| `tastytrade` | >=11.0.0 | TastyTrade API SDK | LEGITIMATE - See supply chain section |
| `humanize` | >=4.12.3 | Human-readable time formatting | LEGITIMATE - Well-known utility |
| `aiolimiter` | ==1.2.1 | Async rate limiting | LEGITIMATE - Simple, focused library |
| `tabulate` | >=0.9.0 | Table formatting for output | LEGITIMATE - Standard data formatting |
| `aiocache` | >=0.12.2 | In-memory caching | LEGITIMATE - Popular async cache |

### Development Dependencies (pyproject.toml:42-51)

| Package | Purpose | Security Notes |
|---------|---------|----------------|
| `ipykernel` | Jupyter integration | Dev only |
| `pydantic-ai` | AI agent framework | Dev only |
| `pyright` | Type checking | Dev only |
| `pytest` | Testing | Dev only |
| `pytest-asyncio` | Async testing | Dev only |
| `python-dotenv` | Env file loading | Dev only - not in production deps |
| `ruff` | Linting | Dev only |
| `typer` | CLI framework | Used in background.py |

### Suspicious Dependencies

**NONE FOUND**

### Version Pinning Analysis

- `aiolimiter==1.2.1` - Fully pinned (most secure)
- `tastytrade>=11.0.0` - Minimum version (acceptable)
- Other deps use `>=` (acceptable for non-critical libraries)
- `uv.lock` contains full SHA256 hashes for all packages (excellent)

### Recommendation
PASS - All dependencies are legitimate and appropriately versioned.

---

## 5. Code Red Flags

**Risk Level:** LOW

### Dangerous Functions Search

| Pattern | Found | Details |
|---------|-------|---------|
| `eval()` | NO | Not in source code |
| `exec()` | NO | Not in source code |
| `subprocess` | NO | Not in source code |
| `pickle` | YES | See analysis below |
| `compile()` | NO | Not in source code |
| `__import__` | NO | Not in source code |
| `os.system` | NO | Not in source code |

### Pickle Usage Analysis

```python
# server.py:13
from aiocache.serializers import PickleSerializer

# server.py:236
@cached(ttl=86400, cache=Cache.MEMORY, serializer=PickleSerializer(), ...)
```

**Risk Assessment:** LOW
- Pickle is used **only for in-memory caching**
- No external data is deserialized
- Cache contains option chain data fetched from TastyTrade
- No pickle files are read from disk

### File System Operations

| Pattern | Found | Risk |
|---------|-------|------|
| `open()` | NO | - |
| `.write()` | NO | - |
| `with open` | NO | - |
| File path manipulation | NO | - |

**Result:** No file system writes outside of Python's standard logging.

### Obfuscated Code

| Pattern | Found |
|---------|-------|
| Base64 encoding | NO |
| Hex-encoded strings | NO |
| Compressed/packed code | NO |
| Unusual string operations | NO |

### Telemetry/Analytics

| Pattern | Found |
|---------|-------|
| Sentry | NO |
| Mixpanel | NO |
| Amplitude | NO |
| Segment | NO |
| Google Analytics | NO |
| Custom telemetry | NO |
| Phone-home behavior | NO |

**Note:** `opentelemetry-api` appears in `uv.lock` as a transitive dependency of `pydantic-ai` (dev dependency only). It is NOT used in production code.

### Recommendation
PASS - No malicious patterns detected.

---

## 6. Supply Chain Security

**Risk Level:** LOW

### Is `tastytrade` the Official Library?

**VERIFIED:** The `tastytrade` package is the well-known community SDK.

| Attribute | Value |
|-----------|-------|
| PyPI Package | [pypi.org/project/tastytrade](https://pypi.org/project/tastytrade/) |
| GitHub Repository | [github.com/tastyware/tastytrade](https://github.com/tastyware/tastytrade) |
| Author | Graeme Holliday (graeme@tastyware.dev) |
| License | MIT |
| Stars | 203+ |
| Status | Production/Stable |

**Important Note:** This is an **unofficial** SDK, not published by TastyTrade Inc. The library states: "This is an unofficial SDK for Tastytrade. There is no implied warranty for any actions and results which arise from using it."

### Typosquatting Check

Searched for similar package names:
- `tastytrade` - Official community SDK (this one)
- `tasty-trade` - Not found
- `tastytrades` - Not found
- `tasty_trade` - Not found

**Result:** No typosquatting risk detected.

### Lock File Analysis

The `uv.lock` file (2200+ lines) contains:
- SHA256 hashes for all package versions
- Exact version pins for reproducible builds
- Upload timestamps for auditability

Example:
```toml
[[package]]
name = "tastytrade"
version = "11.0.2"
sdist = { url = "...", hash = "sha256:8eadb04997257aed6bfd406d9f183ea21dc2f53b917fccf033a4e84bf70153d8", ... }
```

### Recommendation
PASS - Supply chain is secure with proper verification.

---

## 7. Additional Observations

### Logging Practices

The codebase uses Python's standard logging:
- Debug/info level logs for normal operations
- Warning/error logs for failures
- **No sensitive data logged** (verified by grep search)

Log examples that are safe:
```python
logger.info(f"Successfully authenticated with Tastytrade. Found {len(accounts)} account(s).")
logger.info(f"Using specified account: {account.account_number}")  # Account number, not credentials
```

### Rate Limiting

Built-in rate limiting at `server.py:32`:
```python
rate_limiter = AsyncLimiter(2, 1)  # 2 requests per second
```

This prevents API abuse and account lockouts.

### Session Management

Sessions expire after 15 minutes. The code handles this gracefully:
```python
# server.py:97-103
time_until_expiry = (session.session_expiration - now_in_new_york()).total_seconds()
if time_until_expiry < 5:
    session.refresh()
```

---

## 8. Recommendations

### For Read-Only Usage

1. **Remove write functions** from `server.py`:
   - Delete lines 548-780 (place_order through delete_private_watchlist)
   - Or comment out the `@mcp_app.tool()` decorators

2. **Test the modified server** with MCP inspector:
   ```bash
   npx @modelcontextprotocol/inspector uvx tasty-agent
   ```

### For Production Usage

1. **Use environment variables** (never .env files in production)
2. **Monitor logs** for unexpected behavior
3. **Keep dependencies updated** (`uv sync --upgrade`)
4. **Pin the tastytrade version** more strictly if desired:
   ```toml
   tastytrade==11.0.2
   ```

### Optional Security Hardening

1. **Create a read-only fork** and maintain separately
2. **Add order size limits** if using write functions
3. **Implement approval workflow** for orders (not currently in code)

---

## Conclusion

This MCP server is **safe to use** with appropriate precautions. The codebase:

- Does not exfiltrate credentials
- Does not communicate with unauthorized endpoints
- Does not contain malicious code
- Uses legitimate, well-maintained dependencies

**For maximum safety, operate in read-only mode** by removing the write operation functions as described in Section 3.

---

## Appendix: File-by-Line Reference

| File | Lines | Security-Relevant Content |
|------|-------|---------------------------|
| `server.py` | 109-111 | Credential loading (safe) |
| `server.py` | 121 | Session creation with credentials |
| `server.py` | 236 | Pickle serializer usage (safe - memory only) |
| `server.py` | 548-621 | `place_order` - WRITE OPERATION |
| `server.py` | 625-666 | `replace_order` - WRITE OPERATION |
| `server.py` | 670-675 | `delete_order` - WRITE OPERATION |
| `server.py` | 703-773 | `manage_private_watchlist` - WRITE OPERATION |
| `server.py` | 777-780 | `delete_private_watchlist` - WRITE OPERATION |
| `background.py` | 26-27 | Credential loading (safe) |
| `pyproject.toml` | 7-14 | Production dependencies (all safe) |
