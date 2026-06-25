import websockets
import asyncio
import json

async def main():
    
    ws_url = "wss://stream.bybit.com/v5/public/linear" # orderbook snapshot
    #ws_url = "orderbook.full.BTCUSDT" # orderbook delta
    subscriptions = {
            "op": "subscribe",
            "args": ["orderbook.200.BTCUSDT"]
        }
    
    orderbook = {"topic": "orderbook.200.BTCUSDT",
                 "type": "snapshot",
                 "data": [ ["123123","16493.50","0.006"] ]
    }
    
    orderbook["data"].append([0,0])
    print(orderbook)
    
    with open('data/orderbook.json', 'w') as file:
        async with websockets.connect(ws_url) as wss:
            await wss.send(json.dumps(subscriptions))
            while True:
                message = await wss.recv()
                #print(message)
                if message.get("type") == "delta": print('delta')
                new_record = [message.get('ts'), message.get('data').get('b'), message.get('s')]
                orderbook["data"].append([message])
                #file.write(json.dumps(message))
                
            
if __name__ == "__main__":
    asyncio.run(main())