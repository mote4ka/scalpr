import os

from dotenv import load_dotenv

from scalpr_core.paths import PROJECT_ROOT

# .env of the workspace root first: every os.getenv below reads variables from it
load_dotenv(PROJECT_ROOT / ".env")

# LOGS
RICH_TRACEBACK_ENABLE = False

# APIS: keys of the real accounts, used only by scalpr-live
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
HTX_API_KEY = os.getenv("HTX_API_KEY")
HTX_API_SECRET = os.getenv("HTX_API_SECRET")
