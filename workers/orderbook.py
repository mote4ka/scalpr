import websockets
import asyncio
import json
from utils import *
from logger import logger, HandleException
from conn_data import *



async def normalizePrices(asks: list, bids: list) -> :
    """
    Input:
        asks: list of asks with structure [price, qty]
        bids: list of bids
    Output:
        Normalized dom snapshot, where each record in bids & asks are represented as percent of midprice
    """
    if sessionData.get('market_ask') is None or sessionData.get('market_bid') is None:
        logger.warning('Trying Normalize Orderbook with Session Data Empty!')
        return None
    
    ask_price = sessionData.get('market_ask')
    bid_price = sessionData.get('market_bid')
    
    
    normalizedAsks = [ [ round((float(value[0]) - ask_price)/ask_price, 5) , float(value[1])] for value in asks]
    normalizedBids = [ [ round((float(value[0]) - bids)/bids, 5) , float(value[1])] for value in bids]
    
    normalizeDOM = normalizedAsks.extend(normalizedBids)

    return normalizeDOM


async def dataWorker(data: dict) -> None:
    


async def orderbookHandler():
    while True:
        try:
            async with websockets.connect(ws_url) as ws:
                await ws.send(json.dumps(orderbook_sub))
                print("Orderbook: subscription sent")

                async for message in ws:
                    data = json.loads(message)
                    if data.get("op") == "subscribe":
                        logger.info("Subscribed to ORDERBOOK")
                        continue
                    JsonWrite('data/orderbook.json', data)
        except KeyboardInterrupt:
            logger.info('Closing connection...')
        except Exception as e:
            HandleException(e)



