import websockets
import asyncio
import json
from utils import *
from logger import logger

async def main():
    
    ws_url = "wss://stream.bybit.com/v5/public/linear" # orderbook snapshot
    #ws_url = "orderbook.full.BTCUSDT" # orderbook delta
    subscriptions = {
            "op": "subscribe",
            "args": ["orderbook.200.BTCUSDT"]
        }
    
    orderbook = {"topic": "orderbook.200.BTCUSDT",
                 "type": "snapshot",
                 "data": [ { "time": 123123, "asks": [], "bids": []} ]
    }
    
    orderbook["data"].append([0,0])
    print(orderbook)
    

    async with websockets.connect(ws_url) as wss:
        await wss.send(json.dumps(subscriptions))
        while True:
            try:
                message = json.loads(await wss.recv())
                if message == None:
                    logger.info("Message is NULL")
                elif message.get("type") == "delta": print('delta')
                else:
                    new_record = {message.get('ts'), message.get('data').get('a'), message.get('data').get('b')}
                    JsonWrite("data/record.json", new_record)
                    orderbook["data"].append([new_record])
                    JsonWrite("data/orderbook.json",orderbook)
            except Exception as e:
                logger.error(e)

                
            
if __name__ == "__main__":
    asyncio.run(main())