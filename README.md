# trading-bot-Binance
Fully-automatic trading bot on Binance.
To run: python trading_bot.py
Before running, make sure to:
- Check and modify BUY_DCA_PRICE and BUY_DCA_TOKEN lists in trading_bot.py based on your DCA strategy
- Modify your email information in send_message function in trading_bot.py
- Modify get_current_best_coin to choose a coin to trade
- Modify config.py to add your API key and secret from Binance API
- Add or remove coins in trading_coins.py, note that the base stablecoin is USDC
