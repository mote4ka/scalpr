# Connection and Session Data here

ws_url = "wss://stream.bybit.com/v5/public/linear" 

orderbook_sub = {
            "op": "subscribe",
            "args": ["orderbook.1000.ZECUSDT"]
        }
trades_sub = {
            "op": "subscribe",
            "args": ["publicTrade.ZECUSDT"]
        }


sessionData = {
    "ts": None,
    "market_ask": None,
    "market_bid": None,
}


from datetime import datetime
from logger import logger
def initSession(market_ask:float, market_bid:float) -> None:
    sessionData['ts'] = datetime.now().timestamp()
    sessionData['market_ask'] = market_ask
    sessionData['market_bid'] = market_bid

    logger.info("Session initted!")