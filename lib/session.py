import os
import json
from datetime import datetime, timedelta
import keyring
import logging

import tastytrade

logging.basicConfig(level=logging.INFO)


def get_tasty_session(
        username = os.getenv("TASTYTRADE_USERNAME_OR_EMAIL"),
        password = os.getenv("TASTYTRADE_PASSWORD")
    ):
    """
    Get a tastytrade session using credentials and token management.
    Returns an authenticated Session object.
    """

    if not username or not password:
        logging.error("Missing credentials - ensure TASTYTRADE_USERNAME_OR_EMAIL and TASTYTRADE_PASSWORD are set in environment")
        raise ValueError("Missing credentials")

    # Try to get existing token from keyring
    try:
        stored_data = keyring.get_password("tastytrade", username)
        if not stored_data:
            raise ValueError("No stored token")

        token_data = json.loads(stored_data)
        if not isinstance(token_data, dict) or 'token' not in token_data:
            raise ValueError("Invalid token data structure")

        token_time = datetime.fromisoformat(token_data['timestamp'])

        # Check if token is still valid (less than 24 hours old)
        if datetime.now() - token_time < timedelta(hours=24):
            try:
                session = tastytrade.Session(
                    username,
                    remember_token=token_data['token'],
                    remember_me=True
                )

                # Update token after successful creation
                new_token_data = {
                    'token': session.remember_token,
                    'timestamp': datetime.now().isoformat()
                }
                keyring.set_password("tastytrade", username, json.dumps(new_token_data))
                return session
            except Exception:
                # Token failed, will create new one
                pass
    except Exception:
        # Any keyring or token issues, will create new one
        pass

    # Create new session with remember token
    try:
        session = tastytrade.Session(username, password, remember_me=True)
        token_data = {
            'token': session.remember_token,
            'timestamp': datetime.now().isoformat()
        }
        keyring.set_password("tastytrade", username, json.dumps(token_data))
        return session
    except Exception as e:
        logging.error(f"Failed to create new session: {str(e)}")
        raise
