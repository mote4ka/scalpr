import websockets
import asyncio
import json
from utils import *
from logger import logger, HandleException
from workers.conn_data import *



async def normalizePrices(asks: list, bids: list):
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
    
    
    normalizedAsks = [ [ round((float(value[0]) - ask_price)/ask_price*100, 7) , float(value[1])] for value in asks]
    normalizedBids = [ [ round((float(value[0]) -bid_price)/bid_price*100, 7) , float(value[1])] for value in bids]
    #print(normalizedAsks)
    #print(normalizedBids)
    
    normalizedAsks.reverse()
    normalizedAsks.extend(normalizedBids)

    return normalizedAsks


async def dataWorker(data: dict) -> None:
    # init session if not initted
    if sessionData.get("ts") is None:
        initSession(float(data.get('data').get('a')[0][0]), float(data.get('data').get('b')[0][0]))
        
    # initSession before type check because first message is always snapshot 
    # normalize order book if type is snapshot
    if data.get("type") == "snapshot":   
        normalizedDOM = await normalizePrices(data.get('data').get('a'), data.get('data').get('b'))
    
        JsonWrite("data/orderbook.json", data)
        JsonWrite("data/normalized_dom.json", normalizedDOM)
    
    if data.get("type") == "delta":  
        pass   
        


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
                    
                    # pong_received = await ws.ping()  
                    # # only if you want to wait for the corresponding pong  
                    # latency = await pong_received
                    # print(f"{latency * 1000:.2f} ms")
                    
                    await dataWorker(data)
                    
                    
        except KeyboardInterrupt:
            logger.info('Closing connection...')
        except Exception as e:
            HandleException(e)



