import sys

def main():
    """Main entry point for the tasty-agent CLI."""
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        # Run the authentication setup
        try:
            from .auth_cli import auth
        except ImportError:
            print("Error: Could not import authentication module.")
            sys.exit(1)
        
        if auth():
             print("Setup successful.")
        else:
             print("Setup failed.")
        sys.exit(0) # Exit after setup
    else:
        # Run the MCP server
        try:
            from .server import mcp
        except ImportError:
            print("Error: Could not import server module.")
            sys.exit(1)
        
        # mcp.run() handles its own argument parsing for server options
        mcp.run()

if __name__ == "__main__":
    main() 