from datetime import datetime
import time
import websocket, json
import config
import threading
import trading_coins as allcoins
from binance.client import Client
from binance.enums import *
import copy
import smtplib
from email.message import EmailMessage
import math
import logging

# --- Logging configuration: write INFO and above to email.log with timestamps ---
logging.basicConfig(
    filename='email.log',
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s'
)

# Acquire root logger
logger = logging.getLogger()

# --- Thread lock to serialize email sends ---
email_lock = threading.Lock()

# The real DCA percentages are kept secret, this is just an example
BUY_DCA_PRICE = [k / 100 for k in range(100, 89, -1)]
BUY_DCA_TOKEN = [1 / 11 / BUY_DCA_PRICE[k] for k in range(0, 11)]

buy_dca_price = []
buy_dca_token = []

EPSILON = 0.000000001
EPSILON_USDC = 10.0
MINIMUM_USDC_TO_ORDER = 11.0
MINIMUM_USDC_BALANCE = 1000
MAXIMUM_USDC_BALANCE = 1000000
ORDER_CREATE_AT_START = len(BUY_DCA_PRICE) - 1

## To modify when running server
number_of_coins = len(allcoins.coins)
number_of_portfolio = 1 # Can be greater than 1 for diversification
profit_multiplier = 1.03
stoploss_multiplier = 0.0
continue_trading = True # Continue trading after the current position is closed
###

USDC_budget = [0.0 for k in range(0, number_of_portfolio)]
trade_symbol = ["" for k in range(0, number_of_portfolio)]
trade_bucks = [[] for k in range(0, number_of_portfolio)]
last_created_order = [-1 for k in range(0, number_of_portfolio)]
last_filled_order = [-1 for k in range(0, number_of_portfolio)]
stoploss_price = [-1.0 for k in range(0, number_of_portfolio)]
new_cycle = False

sockets = []
allcoins_info = [] # [(symbol, minimum_tradable_asset, asset_decimal, coin_price_decimal, closes = [])]

client = Client(config.API_KEY, config.API_SECRET)

# ----------- Updated send_message with exception logging, thread-safety, and immediate flush -----------
def send_message(text_message):
    """
    Send a notification email; on failure, logs full traceback and message to email.log, flushing immediately.
    """
    EMAIL_ADDRESS = 'your secondary email, used just to send message'
    RECIPIENT_ADDRESS = 'your primary email, used for tracking trading activities'
    EMAIL_PASSWORD = 'aaaaaaaaaabbbbb'  # Please use an app password!

    try:
        # Build a fresh message each time
        em = EmailMessage()
        em['Subject'] = 'Trading bot notification'
        em['From'] = EMAIL_ADDRESS
        em['To'] = RECIPIENT_ADDRESS
        em.set_content(text_message)

        # Serialize SMTP use across threads
        with email_lock:
            with smtplib.SMTP('smtp.gmail.com', 587) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
                smtp.send_message(em)

        logger.info(f"Email sent successfully: {text_message}")
    except Exception as e:
        # Log the text message and exception details with full traceback
        logger.error(f"Failed to send email for message '{text_message}': {e}", exc_info=True)
        # Flush any buffered log records immediately to the file
        for handler in logger.handlers:
            # Only flush file-based handlers
            if hasattr(handler, 'flush'):
                try:
                    handler.flush()
                except Exception:
                    pass

def get_current_time():
    return datetime.now()

def round_down_number(x, decimal_places):
    return math.floor( x * (10.0 ** decimal_places) ) / (10.0 ** decimal_places) 

def round_up_number(x, decimal_places):
    return math.ceil( x * (10.0 ** decimal_places) ) / (10.0 ** decimal_places) 

def get_USDC_balance():
    return float(client.get_asset_balance(asset='USDC')['free'])

def get_USDC_balance_free_and_locked():
    USDC_balance = client.get_asset_balance(asset='USDC')
    return float(USDC_balance['free']) + float(USDC_balance['locked'])

def get_asset_balance(pair_symbol):
    return float(client.get_asset_balance(asset=pair_symbol[:-4])['free'])

def get_asset_balance_free_and_locked(pair_symbol):
    return float(client.get_asset_balance(asset=pair_symbol[:-4])['free']) + float(client.get_asset_balance(asset=pair_symbol[:-4])['locked'])

def get_asset_info_fromfile(ws):
    symbol = ws.url[33:-9].upper()
    for coininfo in allcoins.coins:
        if coininfo[0] == symbol:
            return coininfo

def get_asset_info(ws):
    symbol = ws.url[33:-9].upper()
    for coininfo in allcoins_info:
        if coininfo[0] == symbol:
            return coininfo

def get_trade_index(symbol):
    for i in range(len(allcoins_info)):
        if allcoins_info[i][0] == symbol:
            return i
    return -1

def get_closes(symbol):
    for coin_info in allcoins_info:
        if coin_info[0] == symbol:
            return coin_info[4]

def get_last_filled_order(portfolio_index):
    lastFilledId = len(trade_bucks[portfolio_index])
    openingOrders = client.get_open_orders(symbol = trade_symbol[portfolio_index])
    time.sleep(3)
    for order in openingOrders:
        if order['side'] == "BUY":
            for i in range(len(trade_bucks[portfolio_index])):
                if abs(trade_bucks[portfolio_index][i][3] - float(order['price'])) < EPSILON:
                    lastFilledId = min(lastFilledId, i)
    if lastFilledId != len(trade_bucks[portfolio_index]):
        return lastFilledId - 1
    else:
        return min(len(trade_bucks[portfolio_index]) - 1, last_filled_order[portfolio_index] + ORDER_CREATE_AT_START)

def is_holding_sell_order(portfolio_index):
    openingOrders = client.get_open_orders(symbol = trade_symbol[portfolio_index])
    time.sleep(3)
    for order in openingOrders:
        if order['side'] == "SELL":
            return True
    return False

def get_sell_price(portfolio_index):
    openingOrders = client.get_open_orders(symbol = trade_symbol[portfolio_index])
    time.sleep(3)
    for order in openingOrders:
        if order['side'] == "SELL":
            return float(order['price'])
    return -1

def is_able_to_open_position(portfolio_index):
    if continue_trading == False:
        return False

    if USDC_budget[portfolio_index] < MINIMUM_USDC_BALANCE:
        return False

    if USDC_budget[portfolio_index] > MAXIMUM_USDC_BALANCE:
        return False

    return True

def order(side, symbol, order_type, quantity, quoteOrderQty=-1.0, stopPrice=0.0):
    if order_type == ORDER_TYPE_MARKET:
        if quoteOrderQty < 0.0:
            try:
                print("sending MARKET order: {} - {} - {} - {}".format(symbol, side, order_type, quantity), flush=True)
                order = client.create_order(symbol=symbol, side=side, type=order_type, quantity=quantity)
                time.sleep(3)
                print(order, flush=True)
            except Exception as e:
                print("an exception occured - {}".format(e), flush=True)
                return -1
            return float(order['cummulativeQuoteQty'])
        else:
            try:
                print("sending MARKET order: {} - {} - {} - quoteOrderQty = {}".format(symbol, side, order_type, quoteOrderQty), flush=True)
                order = client.create_order(symbol=symbol, side=side, type=order_type, quoteOrderQty = quoteOrderQty)
                time.sleep(3)
                print(order, flush=True)
            except Exception as e:
                print("an exception occured - {}".format(e), flush=True)
                return -1
            return float(order['cummulativeQuoteQty'])

    if order_type == ORDER_TYPE_LIMIT:
        try:
            print("sending LIMIT order: {} - {} - {} - price = {}, quantity = {}".format(symbol, side, order_type, stopPrice, quantity), flush=True)
            order = client.create_order(symbol=symbol, side=side, type=order_type, price=stopPrice, quantity=quantity, timeInForce=TIME_IN_FORCE_GTC)
            print(order, flush=True)
        except Exception as e:
            print("an exception occured - {}".format(e), flush=True)
            return -1
        return order['orderId']

def cancel_order(symbol, orderId):
    try:
        print("Cancelling order {}.".format(orderId), flush=True)
        client.cancel_order(symbol=symbol, orderId=orderId)
    except Exception as e:
        print("Cancel order failed: {}".format(e), flush=True)
        return False
    return True

def cancel_sell_order(symbol):
    openingOrders = client.get_open_orders(symbol = symbol)
    time.sleep(3)
    for order in openingOrders:
        if order['side'] == "SELL":
            orderId = order['orderId']
            cancel_order(symbol, orderId)

def cancel_all_orders(symbol):
    openingOrders = client.get_open_orders(symbol = symbol)
    time.sleep(3)
    orderIds = []
    for order in openingOrders:
        orderIds.append(copy.deepcopy(order['orderId']))
    for orderId in orderIds:
        cancel_order(symbol, orderId)

# The function below is just a dummy version, the real one is kept secret
def get_current_best_coin(portfolio_index):
    return allcoins_info[0][0]

def update_buy_dca_lists():
    global buy_dca_price, buy_dca_token
    buy_dca_price = BUY_DCA_PRICE
    buy_dca_token = BUY_DCA_TOKEN

def start_a_trade_cycle(portfolio_index, data_file):
    ## Pick a coin
    symbol = get_current_best_coin(portfolio_index)
    if symbol == "":
        return False
    trade_index = get_trade_index(symbol)
    minimum_tradable_asset = allcoins_info[trade_index][1]
    asset_decimal = allcoins_info[trade_index][2]
    coin_price_decimal = allcoins_info[trade_index][3]
    last_price = allcoins_info[trade_index][4][-1]
    ###

    ## Start a new trade cycle
    global USDC_budget, trade_symbol, trade_bucks, last_created_order, last_filled_order, stoploss_price, new_cycle
    update_buy_dca_lists()
    trade_symbol[portfolio_index] = symbol

    USDC_balance = USDC_budget[portfolio_index]
    buy_amount_available = round_down_number((USDC_balance - EPSILON_USDC) / last_price * buy_dca_token[0], asset_decimal)
    while buy_amount_available * last_price < MINIMUM_USDC_TO_ORDER:
        buy_amount_available += minimum_tradable_asset
    USDC_spent = float(order(SIDE_BUY, trade_symbol[portfolio_index], ORDER_TYPE_MARKET, buy_amount_available))
    time.sleep(3)
    print("USDC spent initially = {}".format(USDC_spent), flush=True)
    if USDC_spent >= 0.0:
        new_cycle = True
        last_created_order[portfolio_index] = 0
        last_filled_order[portfolio_index] = 0
        real_amount_afterbuy = get_asset_balance(trade_symbol[portfolio_index])
        time.sleep(3)
        open_price = last_price
        DCA_price = round_up_number( USDC_spent / (round_down_number(real_amount_afterbuy, asset_decimal) * 0.999), coin_price_decimal)
        sell_price = round_up_number( USDC_spent * profit_multiplier / (round_down_number(real_amount_afterbuy, asset_decimal) * 0.999), coin_price_decimal)
        trade_bucks[portfolio_index].append([USDC_spent, buy_amount_available, real_amount_afterbuy, open_price, DCA_price, sell_price, USDC_balance - USDC_spent + sell_price * round_down_number(real_amount_afterbuy, asset_decimal) * 0.999])
        #order(SIDE_SELL, trade_symbol[portfolio_index], ORDER_TYPE_LIMIT, round_down_number(real_amount_afterbuy, asset_decimal), stopPrice=sell_price)

        sum_USDC_to_spend = USDC_spent
        sum_quantity_real = real_amount_afterbuy
        for i in range(1, len(buy_dca_price)):
            buy_price = round_down_number(open_price * buy_dca_price[i], coin_price_decimal)
            qty = round_down_number((USDC_balance - EPSILON_USDC) / last_price * buy_dca_token[i], asset_decimal)
            while qty * buy_price < MINIMUM_USDC_TO_ORDER:
                qty += minimum_tradable_asset
            qty_real = qty * 0.999
            USDC_to_spend = buy_price * qty
            sum_USDC_to_spend += USDC_to_spend
            sum_quantity_real += qty_real
            DCA_price = round_up_number(sum_USDC_to_spend / (round_down_number(sum_quantity_real, asset_decimal) * 0.999), coin_price_decimal)
            sell_price = round_up_number(sum_USDC_to_spend * profit_multiplier / (round_down_number(sum_quantity_real, asset_decimal) * 0.999), coin_price_decimal)
            trade_bucks[portfolio_index].append([USDC_to_spend, qty, sum_quantity_real, buy_price, DCA_price, sell_price, USDC_balance - sum_USDC_to_spend + sell_price * round_down_number(sum_quantity_real, asset_decimal) * 0.999])

        stoploss_price[portfolio_index] = round_up_number(sum_USDC_to_spend * stoploss_multiplier / round_down_number(sum_quantity_real, asset_decimal) / 0.999, coin_price_decimal)

        # Print buy info
        print("{};portfolio {};new position;{}".format(get_current_time(), portfolio_index, trade_symbol[portfolio_index]), end='', file = data_file, flush=True)
        for i in range(len(trade_bucks[portfolio_index])):
            for j in range(len(trade_bucks[portfolio_index][i])):
                if j == 0:
                    print(";", end='', file = data_file, flush=True)
                else:
                    print(",", end='', file = data_file, flush=True)
                print("{}".format(trade_bucks[portfolio_index][i][j]), end='', file = data_file, flush=True)
        print("", file = data_file, flush=True)
        print("({}) (P-{}: coin = {}, old USDC balance = {}, open price = {}, stoploss price = {}, info [(USDC spend, quantity, sum quantity real, buy price, DCA price, sell price, estimated new USDC balance)] = {}.".format(get_current_time(), portfolio_index, trade_symbol[portfolio_index][:-4], USDC_balance, open_price, stoploss_price[portfolio_index], str(trade_bucks[portfolio_index])), flush=True)
        send_message("({}) (P-{}: coin = {}, old USDC balance = {}, open price = {}, stoploss price = {}, info [(USDC spend, quantity, sum quantity real, buy price, DCA price, sell price, estimated new USDC balance)] = {}.".format(get_current_time(), portfolio_index, trade_symbol[portfolio_index][:-4], USDC_balance, open_price, stoploss_price[portfolio_index], str(trade_bucks[portfolio_index])))

        print("{};portfolio {};stoploss update;{}".format(get_current_time(), portfolio_index, stoploss_price[portfolio_index]), file = data_file, flush=True)
        print("P-{}({}): initial stoploss = {}".format(portfolio_index, trade_symbol[portfolio_index][:-4], stoploss_price[portfolio_index]), flush=True)
        send_message("P-{}({}): stoploss = {}".format(portfolio_index, trade_symbol[portfolio_index][:-4], stoploss_price[portfolio_index]))

        for i in range(min(ORDER_CREATE_AT_START + 1, len(trade_bucks[portfolio_index]))):
            if i > 0:
                order(SIDE_BUY, trade_symbol[portfolio_index], ORDER_TYPE_LIMIT, trade_bucks[portfolio_index][i][1], stopPrice=trade_bucks[portfolio_index][i][3])
                last_created_order[portfolio_index] = i
            print("{};portfolio {};create buy order;{}".format(get_current_time(), portfolio_index, i), file = data_file, flush=True)
            if i == 0:
                print("{};portfolio {};filled buy order;0".format(get_current_time(), portfolio_index), file = data_file, flush=True)
                print("P-{}({}): cnt = 0, {} - {} - {} (aim: {})".format(portfolio_index, trade_symbol[portfolio_index][:-4], trade_bucks[portfolio_index][0][3], trade_bucks[portfolio_index][0][4], trade_bucks[portfolio_index][0][5], trade_bucks[portfolio_index][0][6]), flush=True)
                send_message("P-{}({}): cnt = 0, {} - {} - {} (aim: {})".format(portfolio_index, trade_symbol[portfolio_index][:-4], trade_bucks[portfolio_index][0][3], trade_bucks[portfolio_index][0][4], trade_bucks[portfolio_index][0][5], trade_bucks[portfolio_index][0][6]))
        ##
        
        return True

    else:
        return False
    ###

def clean_portfolio(portfolio_index):
    global trade_symbol, trade_bucks, last_created_order, last_filled_order, stoploss_price
    trade_symbol[portfolio_index] = ""
    trade_bucks[portfolio_index] = []
    last_created_order[portfolio_index] = -1
    last_filled_order[portfolio_index] = -1
    stoploss_price[portfolio_index] = -1.0

def calculate_new_budget(portfolio_index, revenue):
    res = USDC_budget[portfolio_index] + revenue
    for i in range(last_filled_order[portfolio_index] + 1):
        res -= trade_bucks[portfolio_index][i][0]
    openingOrders = client.get_open_orders(symbol = trade_symbol[portfolio_index])
    time.sleep(3)
    for i in range(last_filled_order[portfolio_index] + 1, last_created_order[portfolio_index] + 1):
        for order in openingOrders:
            if order['side'] == "BUY" and abs(trade_bucks[portfolio_index][i][3] - float(order['price'])) < EPSILON:
                res -= float(order['cummulativeQuoteQty'])
    return res

def on_open(ws):
    symbol, minimum_tradable_asset, asset_decimal, coin_price_decimal = get_asset_info_fromfile(ws)

    if ws.url[-2:] == "1m":
        print("({}) - Opened {} 1m socket".format(get_current_time(), symbol), flush=True)

    else:
        ## Crawl daily closes
        global allcoins_info
        closes = []    
        for kline in client.get_historical_klines_generator(symbol, Client.KLINE_INTERVAL_1DAY, "1 Aug, 2024"):
            closes.append(float(kline[4]))
        allcoins_info.append((symbol, minimum_tradable_asset, asset_decimal, coin_price_decimal, closes))
        ###

        print("({}) - Opened {} 1d socket".format(get_current_time(), symbol), flush=True)

        ## If it is BTC, read the data file
        if symbol == "BTCUSDC":
            ## Create data file if not exist
            data_file = open("trading_info.txt", "a")
            data_file.close()
            ###

            ## Read current positions from data file
            data_file = open("trading_info.txt")

            global USDC_budget, trade_symbol, trade_bucks, last_created_order, last_filled_order, stoploss_price

            for line in data_file:
                data_line = line[:-1]
                data_parts = data_line.split(';')
                portfolio_index = int(data_parts[1].split(' ')[1])
                if portfolio_index >= number_of_portfolio:
                    continue
                if data_parts[2] == "set budget":
                    USDC_budget[portfolio_index] = float(data_parts[3])
                    clean_portfolio(portfolio_index)
                elif data_parts[2] == "new position":
                    trade_symbol[portfolio_index] = data_parts[3]
                    bucks = []
                    for i in range(4, len(data_parts)):
                        buck_raw = data_parts[i].split(',')
                        buck = []
                        for j in range(len(buck_raw)):
                            buck.append(float(buck_raw[j]))
                        bucks.append(copy.deepcopy(buck))
                    trade_bucks[portfolio_index] = bucks
                elif data_parts[2] == "create buy order":
                    last_created_order[portfolio_index] = int(data_parts[3])
                elif data_parts[2] == "filled buy order":
                    last_filled_order[portfolio_index] = int(data_parts[3])
                elif data_parts[2] == "stoploss update":
                    stoploss_price[portfolio_index] = float(data_parts[3])

            data_file.close()
            ### 

            ## Devide the initial balance into number_of_portfolio parts, or just continue trading based on the read data
            data_file = open("trading_info.txt", "a")
            if USDC_budget[0] < EPSILON:
                USDC_balance = get_USDC_balance()
                time.sleep(3)
                for portfolio_index in range(number_of_portfolio):
                    USDC_budget[portfolio_index] = USDC_balance / number_of_portfolio
                    print("{};portfolio {};set budget;{}".format(get_current_time(), portfolio_index, USDC_budget[portfolio_index]), file = data_file, flush=True)
                    print("P-{}: set budget = {} USDC".format(portfolio_index, USDC_budget[portfolio_index]), flush=True)
                    send_message("P-{}: set budget = {} USDC".format(portfolio_index, USDC_budget[portfolio_index]))
            else:
                for portfolio_index in range(number_of_portfolio):
                    if trade_symbol[portfolio_index] != "":
                        trade_index = get_trade_index(trade_symbol[portfolio_index])
                        minimum_tradable_asset = allcoins_info[trade_index][1]
                        asset_decimal = allcoins_info[trade_index][2]
                        if round_down_number(get_asset_balance_free_and_locked(trade_symbol[portfolio_index]), asset_decimal) + EPSILON < minimum_tradable_asset:
                            cancel_all_orders(trade_symbol[portfolio_index])
                            USDC_budget[portfolio_index] = trade_bucks[portfolio_index][last_filled_order[portfolio_index]][6]
                            print("{};portfolio {};set budget;{}".format(get_current_time(), portfolio_index, USDC_budget[portfolio_index]), file = data_file, flush=True)
                            print("P-{}({}): Completed, new balance = {} USDC".format(portfolio_index, trade_symbol[portfolio_index][:-4], USDC_budget[portfolio_index]), flush=True)
                            send_message("P-{}({}): Completed, new balance = {} USDC".format(portfolio_index, trade_symbol[portfolio_index][:-4], USDC_budget[portfolio_index]))
                            clean_portfolio(portfolio_index)
                    
            data_file.close()
            ###
        ###

def on_close(ws):
    print('--Closed connection--', flush=True)

def on_message(ws, message):
    global trade_symbol, new_cycle

    ## Process and print estimated balance every minute
    if ws.url[-2:] == "1m":
        json_message = json.loads(message)
        candle = json_message['k']
        is_candle_closed = candle['x']
        if is_candle_closed == True:
            global USDC_budget, trade_bucks, last_created_order, last_filled_order, stoploss_price

            ## If it is BTC, scan all portfolios and process
            data_file = open("trading_info.txt", "a")
            for portfolio_index in range(number_of_portfolio):
                if last_created_order[portfolio_index] == -1: # No position at the moment
                    ## Start a new cycle (if available)
                    if is_able_to_open_position(portfolio_index):
                        start_a_trade_cycle(portfolio_index, data_file)
                    ###
                else:
                    ## Create more buy limit orders (if available)
                    old_last_filled_order = copy.deepcopy(last_filled_order[portfolio_index])
                    last_filled_order[portfolio_index] = get_last_filled_order(portfolio_index)
                    for i in range(old_last_filled_order + 1, last_filled_order[portfolio_index] + 1):
                        print("{};portfolio {};filled buy order;{}".format(get_current_time(), portfolio_index, i), file = data_file, flush=True)
                        print("P-{}({}): cnt = {}, {} - {} - {} (aim: {})".format(portfolio_index, trade_symbol[portfolio_index][:-4], i, trade_bucks[portfolio_index][i][3], trade_bucks[portfolio_index][i][4], trade_bucks[portfolio_index][i][5], trade_bucks[portfolio_index][i][6]), flush=True)
                        send_message("P-{}({}): cnt = {}, {} - {} - {} (aim: {})".format(portfolio_index, trade_symbol[portfolio_index][:-4], i, trade_bucks[portfolio_index][i][3], trade_bucks[portfolio_index][i][4], trade_bucks[portfolio_index][i][5], trade_bucks[portfolio_index][i][6]))
                    for i in range(min(old_last_filled_order + ORDER_CREATE_AT_START + 1, len(trade_bucks[portfolio_index])), min(last_filled_order[portfolio_index] + ORDER_CREATE_AT_START + 1, len(trade_bucks[portfolio_index]))):
                        order(SIDE_BUY, trade_symbol[portfolio_index], ORDER_TYPE_LIMIT, trade_bucks[portfolio_index][i][1], stopPrice = trade_bucks[portfolio_index][i][3])
                        last_created_order[portfolio_index] = i
                        print("{};portfolio {};create buy order;{}".format(get_current_time(), portfolio_index, i), file = data_file, flush=True)
                    ###
                        
                    '''
                    ### This part is for limit sell only, which can't handle high volatility cases
                    ## If there's no sell order remaining, cancel all buy orders and update portfolio
                    if is_holding_sell_order(portfolio_index) == False:
                        cancel_all_orders(trade_symbol[portfolio_index])
                        USDC_budget[portfolio_index] = trade_bucks[portfolio_index][last_filled_order[portfolio_index]][6]
                        print("{};portfolio {};set budget;{}".format(get_current_time(), portfolio_index, USDC_budget[portfolio_index]), file = data_file, flush=True)
                        print("P-{}({}): Completed, new balance = {} USDC".format(portfolio_index, trade_symbol[portfolio_index][:-4], USDC_budget[portfolio_index]), flush=True)
                        send_message("P-{}({}): Completed, new balance = {} USDC".format(portfolio_index, trade_symbol[portfolio_index][:-4], USDC_budget[portfolio_index]))
                        clean_portfolio(portfolio_index)
                    ###
                        
                    else:
                    '''
                    asset_balance = get_asset_balance(trade_symbol[portfolio_index])
                    trade_index = get_trade_index(trade_symbol[portfolio_index])
                    asset_decimal = allcoins_info[trade_index][2]
                    last_price = allcoins_info[trade_index][4][-1]
                    '''
                    ### This part is for limit sell only, which can't handle high volatility cases
                    ## If there's free asset, add them to current sell order
                    if round_down_number(asset_balance, asset_decimal) + EPSILON >= allcoins_info[trade_index][1]:
                        cancel_sell_order(trade_symbol[portfolio_index])
                        asset_balance = get_asset_balance(trade_symbol[portfolio_index])
                        order(SIDE_SELL, trade_symbol[portfolio_index], ORDER_TYPE_LIMIT, round_down_number(asset_balance, asset_decimal), stopPrice=trade_bucks[portfolio_index][last_filled_order[portfolio_index]][5])
                    else: # In case new sell order is listed before updating last_filled_order, have to remake sell order
                        sell_price = get_sell_price(portfolio_index)
                        if sell_price >= 0 and abs(sell_price - trade_bucks[portfolio_index][last_filled_order[portfolio_index]][5]) > EPSILON:
                            cancel_sell_order(trade_symbol[portfolio_index])
                            asset_balance = get_asset_balance(trade_symbol[portfolio_index])
                            order(SIDE_SELL, trade_symbol[portfolio_index], ORDER_TYPE_LIMIT, round_down_number(asset_balance, asset_decimal), stopPrice=trade_bucks[portfolio_index][last_filled_order[portfolio_index]][5])
                    ###
                    '''

                    ## If current price >= profit price, take profit by a market sell order
                    if last_price >= trade_bucks[portfolio_index][last_filled_order[portfolio_index]][5]:
                        time.sleep(3)
                        revenue = float(order(SIDE_SELL, trade_symbol[portfolio_index], ORDER_TYPE_MARKET, round_down_number(asset_balance, asset_decimal))) * 0.999
                        USDC_budget[portfolio_index] = calculate_new_budget(portfolio_index, revenue)
                        print("{};portfolio {};set budget;{}".format(get_current_time(), portfolio_index, USDC_budget[portfolio_index]), file = data_file, flush=True)
                        print("P-{}({}): Take profit, new balance = {} USDC".format(portfolio_index, trade_symbol[portfolio_index][:-4], USDC_budget[portfolio_index]), flush=True)
                        send_message("P-{}({}): Take profit, new balance = {} USDC".format(portfolio_index, trade_symbol[portfolio_index][:-4], USDC_budget[portfolio_index]))
                        cancel_all_orders(trade_symbol[portfolio_index])
                        clean_portfolio(portfolio_index)
                    ###
                    ## If current price <= stoploss price, market sell everything
                    elif last_price <= stoploss_price[portfolio_index]:
                        time.sleep(3)
                        revenue = float(order(SIDE_SELL, trade_symbol[portfolio_index], ORDER_TYPE_MARKET, round_down_number(asset_balance, asset_decimal))) * 0.999
                        USDC_budget[portfolio_index] = calculate_new_budget(portfolio_index, revenue)
                        print("{};portfolio {};set budget;{}".format(get_current_time(), portfolio_index, USDC_budget[portfolio_index]), file = data_file, flush=True)
                        print("P-{}({}): Hit stoploss, new balance = {} USDC".format(portfolio_index, trade_symbol[portfolio_index][:-4], USDC_budget[portfolio_index]), flush=True)
                        send_message("P-{}({}): Hit stoploss, new balance = {} USDC".format(portfolio_index, trade_symbol[portfolio_index][:-4], USDC_budget[portfolio_index]))
                        cancel_all_orders(trade_symbol[portfolio_index])
                        clean_portfolio(portfolio_index)
                    ###

            data_file.close()

            estimated_balance_file = open("estimated_balance.txt", "a")
            last_BTC_price = float(candle['c'])
            estimated_balance = get_USDC_balance_free_and_locked()
            time.sleep(3)
            for portfolio_index in range(number_of_portfolio):
                if trade_symbol[portfolio_index] != "":
                    asset_balance = get_asset_balance_free_and_locked(trade_symbol[portfolio_index])
                    time.sleep(3)
                    last_price = get_closes(trade_symbol[portfolio_index])[-1]
                    estimated_balance += asset_balance * last_price * 0.999
            print("{};{};{};".format(get_current_time(), last_BTC_price, estimated_balance), end = '', file = estimated_balance_file, flush = True)
            for portfolio_index in range(number_of_portfolio):
                if trade_symbol[portfolio_index] != "":
                    print("{},".format(trade_symbol[portfolio_index][:-4]), end = '', file = estimated_balance_file, flush = True)
                    
            if new_cycle == True:
                new_cycle = False
                print("*", end = '', file = estimated_balance_file, flush = True)

            print("", file = estimated_balance_file, flush = True)
            estimated_balance_file.close()
    ###

    else:
        ## Save current price to "closes", append closes if a closed candle is met
        closes = get_asset_info(ws)[4]
        json_message = json.loads(message)
        candle = json_message['k']
        is_candle_closed = candle['x']
        last_price = float(candle['c'])
        closes[-1] = last_price
        if is_candle_closed == True:
            closes.append(last_price)
        ###

def activate_socket(socket):
    global sockets
    sockets.append( websocket.WebSocketApp(socket, on_open=on_open, on_close=on_close, on_message=on_message) )
    sockets[-1].run_forever()

if __name__ == "__main__":
    for i in range(1, number_of_coins):
        coin_pair_symbol = allcoins.coins[i][0]
        socket = "wss://stream.binance.com:9443/ws/" + coin_pair_symbol.lower() + "@kline_1d"
        thr = threading.Thread(target=activate_socket, args=(socket,))
        thr.start()
        time.sleep(1)

    time.sleep(10)
    socket = "wss://stream.binance.com:9443/ws/btcusdc@kline_1d"
    thr = threading.Thread(target=activate_socket, args=(socket,))
    thr.start()

    time.sleep(10)
    socket_1m = "wss://stream.binance.com:9443/ws/btcusdc@kline_1m"
    thr_1m = threading.Thread(target=activate_socket, args=(socket_1m,))
    thr_1m.start()
