import websockets
import asyncio
import json
from utils import *
from logger import logger, HandleException
from conn_data import *





async def tradesHandler():
    while True:
        try:
            async with websockets.connect(ws_url) as ws:
                await ws.send(json.dumps(trades_sub))
                print("Trades: subscription sent")

                async for message in ws:
                    data = json.loads(message)
                    if data.get("op") == "subscribe":
                        logger.info("Subscribed to TRADES")
                        continue
                    JsonWrite('data/trades.json', data)
        except KeyboardInterrupt:
            logger.info('Closing connection...')
        except Exception as e:
            HandleException(e)

