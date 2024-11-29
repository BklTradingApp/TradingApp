import os
import requests
import hmac
import hashlib
import base64
import urllib.parse
from dotenv import load_dotenv
from datetime import datetime
import time
import logging
import pandas as pd
import signal
import websocket
import json
import numpy as np  # Added for RSI calculations
from requests.exceptions import HTTPError, Timeout, ConnectionError
import threading  # Import threading for locks and scheduling updates

# Global websocket references
ws_binance = None
ws_kraken = None

# Setup logging to record activity
logging.basicConfig(
    filename='trading_bot.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Load environment variables from .env file
load_dotenv()

# Get API keys and credentials from environment variables
api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_SECRET_KEY")
kraken_api_key = os.getenv("KRAKEN_API_KEY")
kraken_secret_key = os.getenv("KRAKEN_SECRET_KEY")
telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")

# Enable or disable exchanges
enable_binance = os.getenv("ENABLE_BINANCE", "False").lower() == "true"
enable_kraken = os.getenv("ENABLE_KRAKEN", "False").lower() == "true"

# Enable or disable order execution (Monitoring vs. Trading)
trading_active_binance = os.getenv("TRADING_ACTIVE_BINANCE", "False").lower() == "true"
trading_active_kraken = os.getenv("TRADING_ACTIVE_KRAKEN", "False").lower() == "true"

# Base URLs
base_url_binance = "https://api.binance.us"
base_url_kraken = "https://api.kraken.com"

# Configuration
BUY_THRESHOLD = 0.9985
SELL_THRESHOLD = 1.0025
balance_threshold = float(os.getenv("BALANCE_THRESHOLD", "2750"))
ORDER_TIMEOUT = 10  # Minutes
POSITION_SIZE_PERCENTAGE = 0.10  # Use 10% of available balance for initial testing
MAX_RETRY = int(os.getenv("MAX_RETRY", "3"))
SIMULATION_MODE = os.getenv("SIMULATION_MODE", "False").lower() == "true"
RSI_PERIOD = 14  # RSI period for calculations
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
RATE_LIMIT_INTERVAL = 1  # Minimum interval between API calls in seconds
BACKOFF_TIME = int(os.getenv("BACKOFF_TIME", "5"))
LIMIT_BUY_ADJUSTMENT = float(os.getenv("BUY_ADJUSTMENT", "0.002"))
LIMIT_SELL_ADJUSTMENT = float(os.getenv("SELL_ADJUSTMENT", "0.002"))

# For WebSocket URL
ws_url_binance = "wss://stream.binance.us:9443/ws/usdtusd@ticker"
ws_url_kraken = "wss://ws.kraken.com/"

# Order history dataframe
order_history = pd.DataFrame(columns=['Order ID', 'Side', 'Quantity', 'Price', 'Timestamp', 'Status'])
order_history_file = 'order_history.csv'

# Price history for RSI calculation
price_history_binance = []
price_history_kraken = []

# Rate limit tracking
LAST_API_CALL = 0

# Threading lock to protect access to shared resources
price_history_lock = threading.Lock()

# Shared termination flag
terminate_flag = threading.Event()

# Function to send Telegram message
def send_telegram_message(message, parse_mode=None):
    url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": telegram_chat_id,
        "text": message,
        "disable_web_page_preview": True
    }
    if parse_mode:
        payload['parse_mode'] = parse_mode

    backoff_time = BACKOFF_TIME  # Initial backoff time in seconds

    for attempt in range(MAX_RETRY):
        try:
            response = requests.post(url, json=payload, timeout=10)
            if response.status_code == 200:
                logging.info(f"Telegram message sent: {message}")
                return
            elif response.status_code == 401:
                logging.error("Unauthorized. Please check your Telegram bot token.")
                return
            else:
                logging.warning(f"Failed to send Telegram message (Attempt {attempt + 1}). Status Code: {response.status_code}. Response: {response.text}")
                time.sleep(backoff_time)
                backoff_time *= 2  # Exponential backoff
        except (Timeout, ConnectionError) as e:
            logging.error(f"Network error on attempt {attempt + 1} to send Telegram message: {e}")
            time.sleep(backoff_time)
            backoff_time *= 2  # Exponential backoff
    logging.error("Failed to send Telegram message after maximum retries.")

# Function to send initial startup message
def send_startup_message():
    message = "Trading bot started successfully and is now monitoring the market."
    logging.info(message)
    print(message)  # Print to console as well
    send_telegram_message(message)

# Start periodic update with a delay
def periodic_updates():
    if terminate_flag.is_set():
        return

    send_periodic_update()

    # Reschedule the next periodic update
    threading.Timer(3600, periodic_updates).start()

# Graceful shutdown handler
def graceful_exit(signum, frame):
    try:
        message = "Received shutdown signal. Saving order history and exiting gracefully..."
        logging.info(message)
        print(message)  # Add this line to see the output in the console

        terminate_flag.set()  # Signal threads to stop

        # Close WebSocket connections if they are running
        if ws_binance:
            ws_binance.close()
        if ws_kraken:
            ws_kraken.close()

        # Ensure the periodic update thread finishes
        if periodic_thread.is_alive():
            periodic_thread.join()

        if not order_history.empty:
            order_history.to_csv(order_history_file, index=False)
    except Exception as e:
        logging.error(f"Error during graceful shutdown: {e}")
        print(f"Error during graceful shutdown: {e}")  # Add this line
    finally:
        exit(0)

# Register the graceful_exit function to handle different signals
signal.signal(signal.SIGINT, graceful_exit)  # Sent by Ctrl+C
signal.signal(signal.SIGTERM, graceful_exit)  # Sent by `kill` command

# Function to send periodic updates to Telegram
def send_periodic_update():
    try:
        # Add a timestamp to indicate when the update was created
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        with price_history_lock:
            current_price_binance = price_history_binance[-1] if price_history_binance else 'N/A'
            current_price_kraken = price_history_kraken[-1] if price_history_kraken else 'N/A'

        if current_price_binance == 'N/A':
            logging.warning("No price data available for Binance.")
        if current_price_kraken == 'N/A':
            logging.warning("No price data available for Kraken.")

        # Initialize status messages
        binance_status = "<b>Binance Status:</b>\n• Status: <i>Inactive</i>"
        kraken_status = "<b>Kraken Status:</b>\n• Status: <i>Inactive</i>"

        # Binance information
        if enable_binance:
            usd_balance_binance = get_account_balance_binance('USD')
            usdt_balance_binance = get_account_balance_binance('USDT')
            trading_status_binance = "Active" if trading_active_binance else "Monitoring"
            websocket_status_binance = "Connected" if ws_binance and ws_binance.keep_running else "Disconnected"

            # Define emojis based on status
            status_emoji = "🟢" if trading_active_binance else "🟡"
            websocket_emoji = "✅" if websocket_status_binance == "Connected" else "❌"

            binance_status = (
                f"<b>Binance Status:</b>\n"
                f"• Status: <i>{trading_status_binance}</i> {status_emoji}\n"
                f"• USD Balance: <b>${usd_balance_binance:.2f}</b>\n"
                f"• USDT Balance: <b>{usdt_balance_binance:.2f}</b>\n"
                f"• Price: <b>{current_price_binance}</b>\n"
                f"• WebSocket: <i>{websocket_status_binance}</i> {websocket_emoji}"
            )

        # Kraken information
        if enable_kraken:
            usd_balance_kraken = get_account_balance_kraken('ZUSD')
            usdt_balance_kraken = get_account_balance_kraken('USDT')
            trading_status_kraken = "Active" if trading_active_kraken else "Monitoring"
            websocket_status_kraken = "Connected" if ws_kraken and ws_kraken.keep_running else "Disconnected"

            # Define emojis based on status
            status_emoji = "🟢" if trading_active_kraken else "🟡"
            websocket_emoji = "✅" if websocket_status_kraken == "Connected" else "❌"

            kraken_status = (
                f"<b>Kraken Status:</b>\n"
                f"• Status: <i>{trading_status_kraken}</i> {status_emoji}\n"
                f"• USD Balance: <b>${usd_balance_kraken:.2f}</b>\n"
                f"• USDT Balance: <b>{usdt_balance_kraken:.2f}</b>\n"
                f"• Price: <b>{current_price_kraken}</b>\n"
                f"• WebSocket: <i>{websocket_status_kraken}</i> {websocket_emoji}"
            )

        # Construct the message with consistent data for both exchanges
        message = (
            f"📊 <b>Periodic Update ({timestamp}):</b>\n\n"
            f"{binance_status}\n\n"
            f"{kraken_status}"
        )

        send_telegram_message(message, parse_mode='HTML')

    except Exception as e:
        logging.error(f"Failed to send periodic update: {e}")

# Function to make requests with retry logic using exponential backoff
def make_request_with_retries(url, headers, params=None, data=None, method="get"):
    global LAST_API_CALL
    backoff_time = BACKOFF_TIME  # Initial backoff time in seconds

    for attempt in range(MAX_RETRY):
        try:
            # Rate limiting
            current_time = time.time()
            if current_time - LAST_API_CALL < RATE_LIMIT_INTERVAL:
                time.sleep(RATE_LIMIT_INTERVAL - (current_time - LAST_API_CALL))
            LAST_API_CALL = current_time

            # Make the request
            if method.lower() == "get":
                response = requests.get(url, headers=headers, params=params, timeout=10, verify=True)
            elif method.lower() == "post":
                response = requests.post(url, headers=headers, data=data, timeout=10, verify=True)
            elif method.lower() == "delete":
                response = requests.delete(url, headers=headers, params=params, timeout=10, verify=True)
            else:
                raise ValueError("Unsupported request method.")

            if response.status_code == 200:
                return response
            elif response.status_code == 429:  # Rate limit reached
                logging.warning("Rate limit hit. Retrying after waiting for 60 seconds...")
                time.sleep(60)  # Backoff for rate limit
            else:
                logging.error(f"Attempt {attempt + 1} failed: Status Code {response.status_code}. Response: {response.text}")
                time.sleep(backoff_time)
                backoff_time *= 2  # Exponential backoff
        except (Timeout, ConnectionError) as e:
            logging.error(f"Network error on attempt {attempt + 1}: {e}")
            time.sleep(backoff_time)
            backoff_time *= 2  # Exponential backoff

    logging.error("Failed to process the request after maximum retries.")
    return None

# Function to get account balance for Binance
def get_account_balance_binance(asset):
    endpoint = "/api/v3/account"
    timestamp = int(datetime.now().timestamp() * 1000)
    params = {"timestamp": timestamp}
    query_string = "&".join([f"{k}={v}" for k, v in params.items()])
    signature = hmac.new(api_secret.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    headers = {"X-MBX-APIKEY": api_key}
    params["signature"] = signature

    response = make_request_with_retries(f"{base_url_binance}{endpoint}", headers, params)
    if response:
        try:
            account_info = response.json()
            for balance in account_info['balances']:
                if balance['asset'] == asset:
                    return float(balance['free'])  # Get the available (free) balance
            return 0.0  # Return 0 if asset not found
        except Exception as e:
            logging.error(f"An error occurred while parsing account balance: {e}")
            return 0.0
    return 0.0

# Function to get account balance for Kraken
def get_account_balance_kraken(asset):
    endpoint = "/0/private/Balance"
    url = base_url_kraken + endpoint
    nonce = str(int(time.time() * 1000))
    data = {
        'nonce': nonce
    }
    post_data = urllib.parse.urlencode(data)

    # Generate the signature
    message = (nonce + post_data).encode('utf-8')
    sha256_hash = hashlib.sha256(message).digest()
    secret = base64.b64decode(kraken_secret_key)
    hmac_data = endpoint.encode('utf-8') + sha256_hash
    signature = hmac.new(secret, hmac_data, hashlib.sha512)
    signature_b64 = base64.b64encode(signature.digest())

    headers = {
        'API-Key': kraken_api_key,
        'API-Sign': signature_b64.decode()
    }

    response = make_request_with_retries(url, headers=headers, data=post_data, method="post")

    if response:
        try:
            balance_info = response.json()
            if balance_info.get('error') and balance_info['error']:
                logging.error(f"Kraken API returned error: {balance_info['error']}")
                return 0.0
            if "result" in balance_info and asset in balance_info["result"]:
                return float(balance_info["result"][asset])
            else:
                logging.warning(f"Asset {asset} not found in Kraken balance.")
                return 0.0
        except Exception as e:
            logging.error(f"An error occurred while parsing Kraken account balance: {e}")
            return 0.0
    return 0.0

# Function to calculate RSI
def calculate_rsi(prices, period=RSI_PERIOD):
    if len(prices) < period:
        logging.info(f"Not enough data to calculate RSI. Need at least {period} data points.")
        return None

    prices_series = pd.Series(prices)
    delta = prices_series.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)

    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    if rsi.isna().iloc[-1]:
        return None
    else:
        return rsi.iloc[-1]

# Function to determine if order should be placed
def should_place_order(current_price, side, price_history):
    rsi = calculate_rsi(price_history)
    if rsi is None:
        logging.info("Not enough data to calculate RSI.")
        return False

    if side == "BUY":
        if current_price < BUY_THRESHOLD and rsi < RSI_OVERSOLD:
            logging.info(f"Conditions met for BUY order: Price={current_price}, RSI={rsi}")
            return True
    elif side == "SELL":
        if current_price > SELL_THRESHOLD and rsi > RSI_OVERBOUGHT:
            logging.info(f"Conditions met for SELL order: Price={current_price}, RSI={rsi}")
            return True
    return False

# Function to place an instant order for Binance (limit order with small adjustment)
def place_market_order_binance(symbol, side, quantity, current_price):
    if SIMULATION_MODE:
        logging.info(f"Simulating Binance market order placement: {side} {quantity:.2f} of {symbol} at ${current_price:.4f}")
        send_telegram_message(f"Simulating Binance market order placement: {side} {quantity:.2f} of {symbol} at ${current_price:.4f}")
        return None, None

    # Adjust price slightly to ensure the order is filled quickly
    if side == "BUY":
        adjusted_price = current_price + LIMIT_BUY_ADJUSTMENT
    elif side == "SELL":
        adjusted_price = current_price - LIMIT_SELL_ADJUSTMENT
    else:
        raise ValueError("Invalid side, should be either 'BUY' or 'SELL'")

    endpoint = "/api/v3/order"
    timestamp = int(datetime.now().timestamp() * 1000)

    params = {
        "symbol": symbol,
        "side": side,
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": "{:.2f}".format(quantity),
        "price": "{:.4f}".format(adjusted_price),
        "timestamp": timestamp
    }
    query_string = "&".join([f"{k}={v}" for k, v in params.items()])
    signature = hmac.new(api_secret.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    params["signature"] = signature
    headers = {"X-MBX-APIKEY": api_key}

    response = make_request_with_retries(f"{base_url_binance}{endpoint}", headers, params, method="post")
    if response:
        try:
            order_response = response.json()
            if order_response.get("status") == "FILLED":
                logging.info(f"Binance order placed successfully: {order_response}")
                send_telegram_message(f"Binance order placed successfully: {order_response}")
                return order_response['orderId'], datetime.now()
            else:
                logging.warning(f"Order not filled immediately: {order_response}")
                return order_response['orderId'], datetime.now()
        except Exception as e:
            logging.error(f"An error occurred while processing the Binance order response: {e}")
    return None, None

# Function to place an instant order for Kraken (market order)
def place_market_order_kraken(pair, side, volume):
    if SIMULATION_MODE:
        logging.info(f"Simulating Kraken market order placement: {side} {volume:.2f} of {pair}")
        send_telegram_message(f"Simulating Kraken market order placement: {side} {volume:.2f} of {pair}")
        return None, None

    endpoint = "/0/private/AddOrder"
    url = base_url_kraken + endpoint
    nonce = str(int(time.time() * 1000))
    data = {
        'nonce': nonce,
        'ordertype': 'market',
        'type': side.lower(),
        'volume': str(volume),
        'pair': pair  # Use 'USDTUSD' for REST API
    }
    post_data = urllib.parse.urlencode(data)

    # Generate the signature
    message = (nonce + post_data).encode('utf-8')
    sha256_hash = hashlib.sha256(message).digest()
    secret = base64.b64decode(kraken_secret_key)
    hmac_data = endpoint.encode('utf-8') + sha256_hash
    signature = hmac.new(secret, hmac_data, hashlib.sha512)
    signature_b64 = base64.b64encode(signature.digest())

    headers = {
        'API-Key': kraken_api_key,
        'API-Sign': signature_b64.decode()
    }

    response = make_request_with_retries(url, headers=headers, data=post_data, method="post")
    if response:
        try:
            order_response = response.json()
            if order_response.get('error') and not order_response['error']:
                logging.info(f"Kraken order placed successfully: {order_response['result']}")
                send_telegram_message(f"Kraken order placed successfully: {order_response['result']}")
                return order_response['result'], datetime.now()
            else:
                logging.error(f"Kraken order failed: {order_response['error']}")
        except Exception as e:
            logging.error(f"An error occurred while processing the Kraken order response: {e}")
    return None, None

# Function to evaluate the market and place a trade if conditions are met
def evaluate_market_and_execute_orders(current_price, price_history, exchange_name):
    # Calculate RSI
    rsi = calculate_rsi(price_history)
    if rsi is not None:
        logging.info(f"[Evaluation][{exchange_name}] Current price: {current_price}, RSI: {rsi:.2f}")
    else:
        logging.info(f"[Evaluation][{exchange_name}] Current price: {current_price}, RSI: Not enough data")

    # Determine if we should place a BUY order
    if should_place_order(current_price, side="BUY", price_history=price_history):
        if exchange_name == "Binance":
            usd_balance = get_account_balance_binance('USD')
            order_amount = POSITION_SIZE_PERCENTAGE * usd_balance
            check_balance_and_place_order("Binance", current_price, "BUY", usd_balance, order_amount)
        elif exchange_name == "Kraken":
            usd_balance = get_account_balance_kraken('ZUSD')
            order_amount = POSITION_SIZE_PERCENTAGE * usd_balance
            check_balance_and_place_order("Kraken", current_price, "BUY", usd_balance, order_amount)

    # Determine if we should place a SELL order
    if should_place_order(current_price, side="SELL", price_history=price_history):
        if exchange_name == "Binance":
            usdt_balance = get_account_balance_binance('USDT')
            order_amount = POSITION_SIZE_PERCENTAGE * usdt_balance
            check_balance_and_place_order("Binance", current_price, "SELL", usdt_balance, order_amount)
        elif exchange_name == "Kraken":
            usdt_balance = get_account_balance_kraken('USDT')
            order_amount = POSITION_SIZE_PERCENTAGE * usdt_balance
            check_balance_and_place_order("Kraken", current_price, "SELL", usdt_balance, order_amount)

# Function to start WebSocket for Binance
def start_ws_binance():
    global ws_binance

    def on_message(ws, message):
        try:
            data = json.loads(message)
            current_price_binance = float(data['c'])
            logging.info(f"Binance current price: {current_price_binance}")
            update_price_history(price_history_binance, current_price_binance)

            # Check account balance before proceeding
            if enable_binance:
                usd_balance_binance = get_account_balance_binance('USD')
                usdt_balance_binance = get_account_balance_binance('USDT')
                total_balance_binance = usd_balance_binance + usdt_balance_binance

            check_balance_and_evaluate("Binance", current_price_binance, usd_balance_binance, usdt_balance_binance, total_balance_binance, trading_active_binance, price_history_binance)
        except Exception as e:
            logging.error(f"Error in Binance WebSocket on_message: {e}")
            send_telegram_message(f"Binance WebSocket error: {e}")

    def on_error(ws, error):
        logging.error(f"Binance WebSocket error: {error}")
        send_telegram_message(f"Binance WebSocket error: {error}")

    def on_close(ws, close_status_code, close_msg):
        logging.warning(f"Binance WebSocket closed with code {close_status_code}, message: {close_msg}")
        send_telegram_message(f"Binance WebSocket closed with code {close_status_code}, message: {close_msg}")
        reconnect_after_close("Binance", start_ws_binance)

    ws_binance = websocket.WebSocketApp(
        ws_url_binance,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws_binance.run_forever()

# Function to start WebSocket for Kraken
def start_ws_kraken():
    global ws_kraken

    def on_open(ws):
        logging.info("Kraken WebSocket connection opened.")
        subscription_msg = {
            "event": "subscribe",
            "pair": ["USDT/USD"],  # Corrected pair name for WebSocket API
            "subscription": {"name": "ticker"}
        }
        ws.send(json.dumps(subscription_msg))

    def on_message(ws, message):
        try:
            data = json.loads(message)
            if isinstance(data, list) and len(data) > 3 and data[-2] == 'ticker':
                ticker_data = data[1]
                current_price_kraken = float(ticker_data['c'][0])  # 'c' is the closing price
                logging.info(f"Kraken current price: {current_price_kraken}")
                update_price_history(price_history_kraken, current_price_kraken)

                # Check account balance before proceeding
                if enable_kraken:
                    usd_balance_kraken = get_account_balance_kraken('ZUSD')
                    usdt_balance_kraken = get_account_balance_kraken('USDT')
                    total_balance_kraken = usd_balance_kraken + usdt_balance_kraken

                check_balance_and_evaluate("Kraken", current_price_kraken, usd_balance_kraken, usdt_balance_kraken, total_balance_kraken, trading_active_kraken, price_history_kraken)
            elif 'event' in data and data['event'] == 'subscriptionStatus':
                logging.info(f"Kraken subscription status: {data}")
            elif 'event' in data and data['event'] == 'heartbeat':
                logging.debug("Kraken WebSocket heartbeat received.")
            else:
                logging.warning(f"Unhandled message from Kraken: {data}")
        except Exception as e:
            logging.error(f"Error in Kraken WebSocket on_message: {e}")
            send_telegram_message(f"Kraken WebSocket error: {e}")

    def on_error(ws, error):
        logging.error(f"Kraken WebSocket error: {error}")
        send_telegram_message(f"Kraken WebSocket error: {error}")

    def on_close(ws, close_status_code, close_msg):
        logging.warning(f"Kraken WebSocket closed with code {close_status_code}, message: {close_msg}")
        send_telegram_message(f"Kraken WebSocket closed with code {close_status_code}, message: {close_msg}")
        reconnect_after_close("Kraken", start_ws_kraken)

    ws_kraken = websocket.WebSocketApp(
        ws_url_kraken,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws_kraken.run_forever()

# Check if the total balance is above a certain threshold and evaluate the market conditions
def check_balance_and_evaluate(exchange_name, current_price, usd_balance, usdt_balance, total_balance, trading_active, price_history):
    if total_balance < balance_threshold:
        logging.warning(f"Total account balance on {exchange_name} is below ${balance_threshold}. Stopping the bot.")
        send_telegram_message(f"Total account balance on {exchange_name} is below ${balance_threshold}. Stopping the bot.")
        graceful_exit(None, None)

    # Process the price update to determine if buy/sell should be made
    if trading_active:
        evaluate_market_and_execute_orders(current_price, price_history, exchange_name)

def check_balance_and_place_order(exchange, current_price, side, balance, order_amount):
    if balance > 10 and order_amount > 10:
        if exchange == "Binance":
            if side == "BUY":
                place_market_order_binance('USDTUSD', 'BUY', order_amount, current_price + 0.002)
            elif side == "SELL":
                place_market_order_binance('USDTUSD', 'SELL', order_amount, current_price - 0.002)
        elif exchange == "Kraken":
            if side == "BUY":
                place_market_order_kraken('USDTUSD', 'buy', order_amount)
            elif side == "SELL":
                place_market_order_kraken('USDTUSD', 'sell', order_amount)
    else:
        logging.info(f"Not enough balance to place a {side} order on {exchange}.")

def reconnect_after_close(exchange_name, start_ws_func):
    global ws_binance, ws_kraken

    retry_attempts = 0
    max_attempts = 5
    backoff_time = 10

    while retry_attempts < max_attempts and not terminate_flag.is_set():
        logging.warning(f"{exchange_name} WebSocket closed. Attempting to reconnect in {backoff_time} seconds... (Attempt {retry_attempts + 1})")
        send_telegram_message(f"{exchange_name} WebSocket closed. Attempting to reconnect in {backoff_time} seconds... (Attempt {retry_attempts + 1})")

        if exchange_name == "Binance" and ws_binance:
            ws_binance.close()
        elif exchange_name == "Kraken" and ws_kraken:
            ws_kraken.close()

        if terminate_flag.wait(backoff_time):
            return  # Stop trying if termination flag is set

        try:
            start_ws_func()
            return  # Exit if successful
        except Exception as e:
            logging.error(f"Failed to reconnect {exchange_name} WebSocket: {e}")
            retry_attempts += 1
            backoff_time *= 2  # Exponential backoff

    if not terminate_flag.is_set():
        logging.error(f"Maximum reconnection attempts reached for {exchange_name}. Giving up.")
        send_telegram_message(f"Maximum reconnection attempts reached for {exchange_name}. Giving up.")

def update_price_history(price_history_list, price):
    with price_history_lock:
        price_history_list.append(price)  # Add current price to price history
        if len(price_history_list) > RSI_PERIOD + 1:
            price_history_list.pop(0)  # Maintain only the required number of prices for RSI calculation

# Main execution function
def main():
    # Send startup message
    send_startup_message()

    # Start periodic updates in a separate thread
    global periodic_thread
    periodic_thread = threading.Thread(target=periodic_updates, daemon=True)
    periodic_thread.start()

    # Start WebSocket to monitor the market if enabled
    if enable_binance:
        threading.Thread(target=start_ws_binance, daemon=True).start()

    if enable_kraken:
        threading.Thread(target=start_ws_kraken, daemon=True).start()

    # Keep the main thread running
    while not terminate_flag.is_set():
        time.sleep(1)

if __name__ == '__main__':
    main()
