import os
from dotenv import load_dotenv

# .env first: every os.getenv below reads variables from it
load_dotenv()

# LOGS
RICH_TRACEBACK_ENABLE = False

# APIS
BYBIT_API = os.getenv("BYBIT_API")
HTX_API = os.getenv("HTX_API")
