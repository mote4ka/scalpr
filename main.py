import websockets
import asyncio
import json
from utils import *
from logger import logger, HandleException

from workers.orderbook import orderbookHandler
from workers.trades import tradesHandler






async def main():
    
    
    await asyncio.gather(
        orderbookHandler(),
        tradesHandler()
    )
    
    # orderbook = {"topic": "orderbook.200.BTCUSDT",
    #              "type": "snapshot",
    #              "data": [ { "time": 123123, "asks": [], "bids": []} ]
    # }
    
    

    # async with websockets.connect(ws_url) as tickerCon:
    #     await tickerCon.send(json.dumps(subscriptions))
    #     while True:
    #         try:
    #             message = json.loads(await tickerCon.recv())
    #             if message is None:
    #                 logger.info("Message is NULL")
    #             elif message.get("type") == "delta": pass#print('delta')
    #             else:
    #                 #new_record = {message.get('ts'), message.get('data').get('a'), message.get('data').get('b')}
    #                 dom = await normalizePrices(60141.30, message.get('data').get('a'), message.get('data').get('b'))
    #                 JsonWrite("data/record.json", dom)
    #                 #orderbook["data"].append([new_record])
    #                 #JsonWrite("data/orderbook.json",orderbook)
    #         except KeyboardInterrupt:
    #             logger.info('Closing connection...')
    #         except Exception as e:
    #             HandleException(e)
                
    
    # async with websockets.connect(ws_url) as tickerCon:
    #     await tickerCon.send(json.dumps(subscriptions))
    #     while True:

                
            
if __name__ == "__main__":
    asyncio.run(main())