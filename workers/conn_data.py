# Connection and Session Data here

ws_url = "wss://stream.bybit.com/v5/public/linear" 

orderbook_sub = {
            "op": "subscribe",
            "args": ["orderbook.200.BTCUSDT"]
        }
trades_sub = {
            "op": "subscribe",
            "args": ["publicTrade.BTCUSDT"]
        }




sessionData = {
    "ts": None,
    "market_ask": None,
    "market_bid": None,
}