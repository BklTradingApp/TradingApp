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
import numpy as np  # For RSI calculations
from requests.exceptions import HTTPError, Timeout, ConnectionError
import threading  # For locks and scheduling updates
import math
import random  # For simulating price data

# Global websocket references
ws_binance_market = None
ws_binance_userdata = None
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

# Define the symbol you want to subscribe to (Ensure it's valid on Binance US)
symbol_binance = 'usdtusd'  # Updated from 'btcusdt' to 'usdtusd'

# Construct the WebSocket URL for Binance market data
ws_url_binance = f"wss://stream.binance.us:9443/ws/{symbol_binance}@ticker"

# Configuration
BUY_THRESHOLD = 0.9994
SELL_THRESHOLD = 1.0055
MAX_OPEN_ORDERS_PER_EXCHANGE = 5
balance_threshold = float(os.getenv("BALANCE_THRESHOLD", "2750"))
ORDER_TIMEOUT = 10  # Minutes
POSITION_SIZE_PERCENTAGE = 0.10  # Use 10% of available balance for BUY orders
MAX_RETRY = int(os.getenv("MAX_RETRY", "3"))
SIMULATION_MODE = os.getenv("SIMULATION_MODE", "False").lower() == "true"
RSI_PERIOD = 14  # RSI period for calculations
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
RATE_LIMIT_INTERVAL = 1  # Minimum interval between API calls in seconds
BACKOFF_TIME = int(os.getenv("BACKOFF_TIME", "5"))
LIMIT_BUY_ADJUSTMENT = float(os.getenv("BUY_ADJUSTMENT", "0.002"))
LIMIT_SELL_ADJUSTMENT = float(os.getenv("SELL_ADJUSTMENT", "0.002"))
USE_RSI = os.getenv("USE_RSI", "False").lower() == "true"
COOLDOWN_TIME = 300  # 5 minutes

# Order Tracking
open_orders = {
    "Binance": {
        "BUY": {"orderId": None, "timestamp": None, "linked_sell_order": None},
        "SELL": {"orderId": None, "timestamp": None, "linked_buy_order": None}
    },
    "Kraken": {
        "BUY": {"orderId": None, "timestamp": None, "linked_sell_order": None},
        "SELL": {"orderId": None, "timestamp": None, "linked_buy_order": None}
    }
}

# Threading locks and shared resources
open_orders_lock = threading.Lock()

# Initialize price_crossed dictionary
price_crossed = {
    "Binance": {"BUY": False, "SELL": False},
    "Kraken": {"BUY": False, "SELL": False}
}

# Initialize order cooldowns
order_cooldowns = {
    "Binance": {"BUY": 0, "SELL": 0},
    "Kraken": {"BUY": 0, "SELL": 0}
}

# Order history dataframe
order_history = pd.DataFrame(columns=['Order ID', 'Side', 'Quantity', 'Price', 'Timestamp', 'Status'])
order_history_file = 'order_history.csv'

# Price history for RSI calculation
price_history_binance = []
price_history_kraken = []

# Threading lock to protect access to price history
price_history_lock = threading.Lock()

# Rate limit tracking
LAST_API_CALL = 0

# Shared termination flag
terminate_flag = threading.Event()

# Function to send Telegram message
def send_telegram_message(message, parse_mode=None):
    if not telegram_bot_token or not telegram_chat_id:
        logging.error("Telegram bot token or chat ID not set.")
        return

    url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": telegram_chat_id,
        "text": message,
        "disable_web_page_preview": True
    }
    if parse_mode:
        payload['parse_mode'] = parse_mode

    # Prefix message with [SIMULATION] if in simulation mode
    if SIMULATION_MODE:
        message = f"[SIMULATION] {message}"

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
                logging.warning(f"Failed to send Telegram message (Attempt {attempt + 1}). "
                                f"Status Code: {response.status_code}. Response: {response.text}")
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

def get_binance_symbol_info(symbol):
    try:
        response = requests.get(f"{base_url_binance}/api/v3/exchangeInfo", timeout=10)
        response.raise_for_status()
        exchange_info = response.json()
        for s in exchange_info['symbols']:
            if s['symbol'].lower() == symbol.lower():
                return s
        logging.error(f"Symbol {symbol} not found in Binance exchange info.")
    except Exception as e:
        logging.error(f"Error fetching Binance exchange info: {e}")
    return None

def adjust_quantity(quantity, step_size, min_qty):
    """Adjusts the quantity to comply with Binance's step size and minimum quantity."""
    if quantity < min_qty:
        return None  # Quantity too small
    # Calculate the number of steps
    steps = math.floor(quantity / step_size)
    adjusted_quantity = steps * step_size
    # Ensure adjusted_quantity meets min_qty
    if adjusted_quantity < min_qty:
        return None
    return adjusted_quantity

# Graceful shutdown handler
def graceful_exit(signum, frame):
    try:
        message = "Received shutdown signal. Saving order history and exiting gracefully..."
        logging.info(message)
        print(message)  # Output to console
        send_telegram_message(message)  # Send shutdown message via Telegram

        terminate_flag.set()  # Signal threads to stop

        # Close WebSocket connections if they are running
        if ws_binance_market:
            ws_binance_market.close()
        if ws_binance_userdata:
            ws_binance_userdata.close()
        if ws_kraken:
            ws_kraken.close()

        # Ensure the periodic update thread finishes
        if 'periodic_thread' in globals() and periodic_thread.is_alive():
            periodic_thread.join()

        if not order_history.empty:
            order_history.to_csv(order_history_file, index=False)
            logging.info(f"Order history saved to {order_history_file}.")
        send_telegram_message("Trading bot has been shut down gracefully.")
    except Exception as e:
        logging.error(f"Error during graceful shutdown: {e}")
        print(f"Error during graceful shutdown: {e}")  # Output to console
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
            total_balance_binance = usd_balance_binance + usdt_balance_binance
            trading_status_binance = "Active" if trading_active_binance else "Monitoring"
            websocket_status_binance = "Connected" if ws_binance_market and ws_binance_market.keep_running else "Disconnected"

            # Define emojis based on status
            status_emoji = "🟢" if trading_active_binance else "🟡"
            websocket_emoji = "✅" if websocket_status_binance == "Connected" else "❌"

            # Calculate RSI
            with price_history_lock:
                rsi_binance = calculate_rsi(price_history_binance)
            if rsi_binance is not None:
                rsi_binance_str = f"{rsi_binance:.2f}"
            else:
                rsi_binance_str = "Not enough data"

            # Include Buy and Sell thresholds and RSI status
            buy_threshold_str = f"{BUY_THRESHOLD:.4f}"
            sell_threshold_str = f"{SELL_THRESHOLD:.4f}"
            rsi_usage_str = "Enabled" if USE_RSI else "Disabled"

            binance_status = (
                f"<b>Binance Status:</b>\n"
                f"• Status: <i>{trading_status_binance}</i> {status_emoji}\n"
                f"• USD Balance: <b>${usd_balance_binance:.2f}</b>\n"
                f"• USDT Balance: <b>{usdt_balance_binance:.2f}</b>\n"
                f"• Price: <b>{current_price_binance}</b>\n"
                f"• RSI: <b>{rsi_binance_str}</b>\n"
                f"• RSI Usage: <b>{rsi_usage_str}</b>\n"
                f"• Buy Threshold: <b>{buy_threshold_str}</b>\n"
                f"• Sell Threshold: <b>{sell_threshold_str}</b>\n"
                f"• WebSocket: <i>{websocket_status_binance}</i> {websocket_emoji}"
            )

        # Kraken information
        if enable_kraken:
            usd_balance_kraken = get_account_balance_kraken('ZUSD')
            usdt_balance_kraken = get_account_balance_kraken('USDT')
            total_balance_kraken = usd_balance_kraken + usdt_balance_kraken
            trading_status_kraken = "Active" if trading_active_kraken else "Monitoring"
            websocket_status_kraken = "Connected" if ws_kraken and ws_kraken.keep_running else "Disconnected"

            # Define emojis based on status
            status_emoji = "🟢" if trading_active_kraken else "🟡"
            websocket_emoji = "✅" if websocket_status_kraken == "Connected" else "❌"

            # Calculate RSI
            with price_history_lock:
                rsi_kraken = calculate_rsi(price_history_kraken)
            if rsi_kraken is not None:
                rsi_kraken_str = f"{rsi_kraken:.2f}"
            else:
                rsi_kraken_str = "Not enough data"

            # Include Buy and Sell thresholds
            buy_threshold_str = f"{BUY_THRESHOLD:.4f}"
            sell_threshold_str = f"{SELL_THRESHOLD:.4f}"
            rsi_usage_str = "Enabled" if USE_RSI else "Disabled"

            kraken_status = (
                f"<b>Kraken Status:</b>\n"
                f"• Status: <i>{trading_status_kraken}</i> {status_emoji}\n"
                f"• USD Balance: <b>${usd_balance_kraken:.2f}</b>\n"
                f"• USDT Balance: <b>{usdt_balance_kraken:.2f}</b>\n"
                f"• Price: <b>{current_price_kraken}</b>\n"
                f"• RSI: <b>{rsi_kraken_str}</b>\n"
                f"• RSI Usage: <b>{rsi_usage_str}</b>\n"
                f"• Buy Threshold: <b>{buy_threshold_str}</b>\n"
                f"• Sell Threshold: <b>{sell_threshold_str}</b>\n"
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
        logging.error(f"Error while sending periodic update: {e}")


# Function to make requests with retry logic using exponential backoff
def make_request_with_retries(url, headers, params=None, data=None, method="get"):
    global LAST_API_CALL

    if SIMULATION_MODE:
        logging.info(f"SIMULATION_MODE: Skipping API request {method.upper()} {url}")
        # Return a mock response object if needed
        class MockResponse:
            def __init__(self, status_code, json_data=None):
                self.status_code = status_code
                self._json = json_data or {}

            def json(self):
                return self._json

        # Customize the mock response based on the endpoint or method
        if "userDataStream" in url and method.lower() == "post":
            return MockResponse(200, {"listenKey": "mock_listen_key"})
        elif "userDataStream" in url and method.lower() == "put":
            return MockResponse(200, {})
        elif "/api/v3/account" in url and method.lower() == "get":
            return MockResponse(200, {
                "balances": [
                    {"asset": "USD", "free": "0.75"},
                    {"asset": "USDT", "free": "0.21"}
                ]
            })
        elif "/api/v3/order" in url and method.lower() == "get":
            # Mock order status
            return MockResponse(200, {"status": "FILLED"})
        # Add more mock responses as needed
        return MockResponse(200, {})

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
            elif method.lower() == "put":
                response = requests.put(url, headers=headers, params=params, data=data, timeout=10, verify=True)
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
    params = {"timestamp": timestamp, "recvWindow": 5000}  # Increased recvWindow
    query_string = "&".join([f"{k}={v}" for k, v in params.items()])
    signature = hmac.new(api_secret.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    params["signature"] = signature
    headers = {"X-MBX-APIKEY": api_key}

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
                send_telegram_message(f"Kraken API returned error: {balance_info['error']}")
                return 0.0
            if "result" in balance_info and asset in balance_info["result"]:
                return float(balance_info["result"][asset])
            else:
                logging.warning(f"Asset {asset} not found in Kraken balance.")
                return 0.0
        except Exception as e:
            logging.error(f"An error occurred while parsing Kraken account balance: {e}")
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
def should_place_order(current_price, side, price_history, exchange):
    previous_price = price_history[-2] if len(price_history) >= 2 else current_price
    crossed = False

    if side == "BUY":
        if previous_price > BUY_THRESHOLD and current_price <= BUY_THRESHOLD:
            crossed = True
    elif side == "SELL":
        if previous_price < SELL_THRESHOLD and current_price >= SELL_THRESHOLD:
            crossed = True

    if crossed:
        price_crossed[exchange][side] = True
        return True
    return False

# Function to place a limit order for Binance
def place_limit_order_binance(symbol, side, quantity, price):
    if SIMULATION_MODE:
        logging.info(f"Simulating Binance LIMIT order placement: {side} {quantity} of {symbol} at ${price:.4f}")
        send_telegram_message(f"Simulating Binance LIMIT order placement: {side} {quantity} of {symbol} at ${price:.4f}")
        # Return mock order_id and timestamp
        return "simulated_order_id", datetime.now()

    # Adjust price and quantity formatting
    adjusted_price_str = "{:.4f}".format(price)
    adjusted_quantity = "{:.6f}".format(quantity)  # Adjust decimal places as per STEP_SIZE

    endpoint = "/api/v3/order"
    timestamp = int(datetime.now().timestamp() * 1000)

    params = {
        "symbol": symbol.upper(),
        "side": side,
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": adjusted_quantity,
        "price": adjusted_price_str,
        "timestamp": timestamp,
        "recvWindow": 5000  # Increased recvWindow
    }
    query_string = urllib.parse.urlencode(params)
    signature = hmac.new(api_secret.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    params["signature"] = signature
    headers = {"X-MBX-APIKEY": api_key}

    response = make_request_with_retries(f"{base_url_binance}{endpoint}", headers, params, method="post")
    if response:
        try:
            order_response = response.json()
            if 'code' in order_response:
                # Binance API returned an error
                error_message = order_response.get('msg', 'No error message provided.')
                logging.error(f"Binance API Error: {order_response['code']} - {error_message}")
                send_telegram_message(f"Binance API Error: {order_response['code']} - {error_message}")
                return None, None
            elif order_response.get("status") == "FILLED":
                logging.info(f"Binance LIMIT order placed successfully and filled: {order_response}")
                send_telegram_message(f"Binance LIMIT order placed successfully and filled: {order_response}")
                return order_response.get('orderId'), datetime.now()
            else:
                logging.warning(f"Order not filled immediately: {order_response}")
                send_telegram_message(f"Binance LIMIT order not filled immediately: {order_response}")
                return order_response.get('orderId'), datetime.now()
        except Exception as e:
            logging.error(f"An error occurred while processing the Binance order response: {e}")
            send_telegram_message(f"Error placing LIMIT order on Binance: {e}")
    else:
        logging.error("Failed to receive response from Binance when placing LIMIT order.")
        send_telegram_message("Failed to receive response from Binance when placing LIMIT order.")
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
        'pair': pair  # Use 'USDTUSD' for Kraken API
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
                send_telegram_message(f"Kraken order failed: {order_response['error']}")
        except Exception as e:
            logging.error(f"An error occurred while processing the Kraken order response: {e}")
    return None, None

# Function to evaluate the market and place a trade if conditions are met
def evaluate_market_and_execute_orders(current_price, price_history, exchange_name):
    if USE_RSI:
        # Calculate RSI
        rsi = calculate_rsi(price_history)
        if rsi is not None:
            logging.info(f"[Evaluation][{exchange_name}] Current price: {current_price}, RSI: {rsi:.2f}")
        else:
            logging.info(f"[Evaluation][{exchange_name}] Current price: {current_price}, RSI: Not enough data")
    else:
        rsi = None  # RSI not used
        logging.info(f"[Evaluation][{exchange_name}] Current price: {current_price} (RSI not used)")

    # Determine if we should place a BUY order
    if should_place_order(current_price, side="BUY", price_history=price_history, exchange=exchange_name):
        if exchange_name == "Binance":
            check_balance_and_place_order("Binance", "BUY")
        elif exchange_name == "Kraken":
            check_balance_and_place_order("Kraken", "BUY")

    # Determine if we should place a SELL order
    if should_place_order(current_price, side="SELL", price_history=price_history, exchange=exchange_name):
        if exchange_name == "Binance":
            check_balance_and_place_order("Binance", "SELL")
        elif exchange_name == "Kraken":
            check_balance_and_place_order("Kraken", "SELL")

# Function to start WebSocket for Binance Market Data
def start_ws_binance():
    global ws_binance_market

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
                logging.debug(f"Binance Balances - USD: {usd_balance_binance}, USDT: {usdt_balance_binance}, Total: {total_balance_binance}")

            evaluate_market_and_execute_orders(current_price_binance, price_history_binance, "Binance")
        except Exception as e:
            logging.error(f"Error in Binance Market WebSocket on_message: {e}")
            send_telegram_message(f"Binance Market WebSocket error: {e}")

    def on_error(ws, error):
        logging.error(f"Binance Market WebSocket error: {error}")
        send_telegram_message(f"Binance Market WebSocket error: {error}")

    def on_close(ws, close_status_code, close_msg):
        logging.warning(f"Binance Market WebSocket closed with code {close_status_code}, message: {close_msg}")
        send_telegram_message(f"Binance Market WebSocket closed with code {close_status_code}, message: {close_msg}")
        reconnect_after_close("Binance Market Data", start_ws_binance)

    ws_binance_market = websocket.WebSocketApp(
        ws_url_binance,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws_binance_market.run_forever()

# Function to start WebSocket for Binance User Data Stream
def start_ws_binance_user_data(listen_key):
    global ws_binance_userdata

    ws_url = f"wss://stream.binance.us:9443/ws/{listen_key}"
    ws_binance_userdata = websocket.WebSocketApp(
        ws_url,
        on_message=on_message_binance_userdata,
        on_error=on_error_binance_userdata,
        on_close=on_close_binance_userdata
    )
    ws_binance_userdata.run_forever()

def on_message_binance_userdata(ws, message):
    try:
        data = json.loads(message)
        event_type = data.get("e")

        if event_type == "executionReport":
            order_id = data.get("i")
            order_status = data.get("X")  # 'X' stands for Order Status
            side = data.get("S")  # 'BUY' or 'SELL'

            if order_status == "FILLED":
                logging.info(f"Order {order_id} {side} on Binance is FILLED.")
                send_telegram_message(f"Order {order_id} {side} on Binance is FILLED.")

                with open_orders_lock:
                    if side == "BUY":
                        # Place corresponding SELL order
                        place_sell_order_binance()
                    elif side == "SELL":
                        # Optionally, place a new BUY order to continue the loop
                        place_buy_order_binance()

                    # Clear the fulfilled order from open_orders
                    open_orders["Binance"][side] = {
                        "orderId": None,
                        "timestamp": None,
                        "linked_sell_order": None
                    } if side == "SELL" else {
                        "orderId": None,
                        "timestamp": None,
                        "linked_sell_order": None
                    }

        # Handle other event types if necessary

    except Exception as e:
        logging.error(f"Error processing Binance User Data Stream message: {e}")
        send_telegram_message(f"Error processing Binance User Data Stream message: {e}")

def on_error_binance_userdata(ws, error):
    logging.error(f"Binance User Data WebSocket error: {error}")
    send_telegram_message(f"Binance User Data WebSocket error: {error}")

def on_close_binance_userdata(ws, close_status_code, close_msg):
    logging.warning(f"Binance User Data WebSocket closed with code {close_status_code}, message: {close_msg}")
    send_telegram_message(f"Binance User Data WebSocket closed with code {close_status_code}, message: {close_msg}")
    reconnect_after_close("Binance User Data", lambda: start_ws_binance_user_data(listen_key_binance))

# Function to create a listen key for Binance User Data Stream
def create_listen_key_binance():
    endpoint = "/api/v3/userDataStream"
    headers = {"X-MBX-APIKEY": api_key}
    response = make_request_with_retries(f"{base_url_binance}{endpoint}", headers=headers, method="post")
    if response and response.status_code == 200:
        listen_key = response.json().get("listenKey")
        logging.info(f"Binance Listen Key created: {listen_key}")
        return listen_key
    else:
        logging.error(f"Failed to create Binance Listen Key: {response.text if response else 'No response'}")
        send_telegram_message(f"Failed to create Binance Listen Key: {response.text if response else 'No response'}")
    return None

# Function to keep the listen key alive
def keep_listen_key_alive_binance(listen_key):
    endpoint = "/api/v3/userDataStream"
    headers = {"X-MBX-APIKEY": api_key}
    params = {"listenKey": listen_key}
    response = make_request_with_retries(f"{base_url_binance}{endpoint}", headers=headers, params=params, method="put")
    if response and response.status_code == 200:
        logging.info("Binance Listen Key refreshed successfully.")
        return True
    else:
        logging.error(f"Failed to refresh Binance Listen Key: {response.text if response else 'No response'}")
        return False

# Function to maintain the listen key by refreshing it periodically
def maintain_listen_key_binance(listen_key):
    retry_attempts = 0
    max_attempts = 5
    backoff_time = 10

    while retry_attempts < max_attempts and not terminate_flag.is_set():
        success = keep_listen_key_alive_binance(listen_key)
        if not success:
            logging.error("Could not refresh Binance Listen Key. Exiting...")
            send_telegram_message("Could not refresh Binance Listen Key. Exiting...")
            graceful_exit(None, None)
        time.sleep(1800)  # Refresh every 30 minutes

# Function to place a BUY order on Binance
def place_buy_order_binance():
    usd_balance = get_account_balance_binance('USD')
    order_amount = POSITION_SIZE_PERCENTAGE * usd_balance
    current_price = BUY_THRESHOLD  # As per your strategy

    # Calculate the USDT quantity to buy
    raw_quantity = order_amount / current_price
    quantity = adjust_quantity(raw_quantity, STEP_SIZE, MIN_QUANTITY)

    if quantity:
        # Place LIMIT BUY order at BUY_THRESHOLD
        order_id, timestamp = place_limit_order_binance(symbol_binance, 'BUY', quantity, current_price)

        if order_id:
            with open_orders_lock:
                open_orders["Binance"]["BUY"] = {
                    "orderId": order_id,
                    "timestamp": timestamp,
                    "linked_sell_order": None
                }
            logging.info(f"Placed BUY order on Binance: Order ID={order_id}, Quantity={quantity}, Price=${current_price}")
            send_telegram_message(f"Placed BUY order on Binance: Order ID={order_id}, Quantity={quantity}, Price=${current_price}")
    else:
        logging.info(f"Calculated quantity {raw_quantity:.6f} is less than the minimum required ({MIN_QUANTITY}) for Binance.")
        send_telegram_message(f"Calculated quantity {raw_quantity:.6f} is less than the minimum required ({MIN_QUANTITY}) for Binance.")

# Function to place a SELL order on Binance
def place_sell_order_binance():
    usdt_balance = get_account_balance_binance('USDT')
    order_amount = usdt_balance  # Sell entire USDT balance
    current_price = SELL_THRESHOLD  # As per your strategy

    # Adjust quantity to comply with Binance's requirements
    quantity = adjust_quantity(order_amount, STEP_SIZE, MIN_QUANTITY)

    if quantity:
        # Place LIMIT SELL order at SELL_THRESHOLD
        order_id, timestamp = place_limit_order_binance(symbol_binance, 'SELL', quantity, current_price)

        if order_id:
            with open_orders_lock:
                open_orders["Binance"]["SELL"] = {
                    "orderId": order_id,
                    "timestamp": timestamp,
                    "linked_buy_order": open_orders["Binance"]["BUY"]["orderId"]
                }
            logging.info(f"Placed SELL order on Binance: Order ID={order_id}, Quantity={quantity}, Price=${current_price}")
            send_telegram_message(f"Placed SELL order on Binance: Order ID={order_id}, Quantity={quantity}, Price=${current_price}")
    else:
        logging.info(f"Calculated quantity {order_amount:.6f} is less than the minimum required ({MIN_QUANTITY}) for Binance.")
        send_telegram_message(f"Calculated quantity {order_amount:.6f} is less than the minimum required ({MIN_QUANTITY}) for Binance.")

# Function to evaluate the market and place a trade if conditions are met
def check_balance_and_place_order(exchange, side):
    with open_orders_lock:
        if open_orders[exchange][side]['orderId'] is not None:
            logging.info(f"An open {side} order already exists on {exchange}. Skipping new order placement.")
            return

    if not check_and_update_cooldown(exchange, side):
        logging.info(f"Cooldown period active for {side} orders on {exchange}. Skipping order placement.")
        return

    with open_orders_lock:
        total_open_orders = sum(1 for order in open_orders[exchange].values() if order['orderId'] is not None)
        if total_open_orders >= MAX_OPEN_ORDERS_PER_EXCHANGE:
            logging.info(f"Maximum open orders reached on {exchange}. Skipping new order placement.")
            return

    if exchange == "Binance":
        if side == "BUY":
            place_buy_order_binance()
        elif side == "SELL":
            place_sell_order_binance()
    elif exchange == "Kraken":
        # Implement similar logic for Kraken if needed
        pass

# Function to reconnect WebSockets after closure
def reconnect_after_close(exchange_name, start_ws_func):
    retry_attempts = 0
    max_attempts = 5
    backoff_time = 10

    while retry_attempts < max_attempts and not terminate_flag.is_set():
        logging.warning(f"{exchange_name} WebSocket closed. Attempting to reconnect in {backoff_time} seconds... (Attempt {retry_attempts + 1})")
        send_telegram_message(f"{exchange_name} WebSocket closed. Attempting to reconnect in {backoff_time} seconds... (Attempt {retry_attempts + 1})")

        if exchange_name.startswith("Binance"):
            if "User Data" in exchange_name:
                if ws_binance_userdata:
                    ws_binance_userdata.close()
            else:
                if ws_binance_market:
                    ws_binance_market.close()
        elif exchange_name.startswith("Kraken"):
            if ws_kraken:
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
        send_telegram_message(f"Maximum reconnection attempts reached for {exchange_name}. Shutting down the bot.")
        graceful_exit(None, None)  # Shut down the bot

# Function to update price history
def update_price_history(price_history_list, price):
    with price_history_lock:
        price_history_list.append(price)  # Add current price to price history
        if len(price_history_list) > RSI_PERIOD + 1:
            price_history_list.pop(0)  # Maintain only the required number of prices for RSI calculation

# Function to get order status for Binance
def get_order_status_binance(symbol, order_id):
    endpoint = "/api/v3/order"
    timestamp = int(datetime.now().timestamp() * 1000)
    params = {
        "symbol": symbol.upper(),
        "orderId": order_id,
        "timestamp": timestamp,
        "recvWindow": 5000  # Increased recvWindow
    }
    query_string = urllib.parse.urlencode(params)
    signature = hmac.new(api_secret.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    params["signature"] = signature
    headers = {"X-MBX-APIKEY": api_key}

    response = make_request_with_retries(f"{base_url_binance}{endpoint}", headers, params, method="get")
    if response:
        try:
            order_info = response.json()
            return order_info.get("status")
        except Exception as e:
            logging.error(f"Error fetching Binance order status: {e}")
    return None

# Function to monitor orders via polling (can be deprecated with WebSocket)
def monitor_orders():
    while not terminate_flag.is_set():
        with open_orders_lock:
            for exchange in open_orders:
                # Skip if exchange is not enabled
                if (exchange == "Binance" and not enable_binance) or (exchange == "Kraken" and not enable_kraken):
                    continue
                for side in open_orders[exchange]:
                    order = open_orders[exchange][side]
                    if order and not (exchange == "Binance" and side == "SELL"):
                        status = get_order_status_binance(symbol_binance, order['orderId'])
                        if status in ["FILLED", "CANCELED"]:
                            logging.info(f"Order {order['orderId']} on {exchange} {side} is {status}.")
                            send_telegram_message(f"Order {order['orderId']} on {exchange} {side} is {status}.")
                            open_orders[exchange][side] = {
                                "orderId": None,
                                "timestamp": None,
                                "linked_sell_order": None
                            } if side == "BUY" else {
                                "orderId": None,
                                "timestamp": None,
                                "linked_buy_order": None
                            }
        time.sleep(60)  # Check every minute

# Function to check and update cooldown
def check_and_update_cooldown(exchange, side):
    current_time = time.time()
    with open_orders_lock:
        if current_time < order_cooldowns[exchange][side]:
            return False
        order_cooldowns[exchange][side] = current_time + COOLDOWN_TIME
    return True

# Function to simulate price updates
def simulate_price_update():
    while not terminate_flag.is_set():
        simulated_price = round(random.uniform(0.98, 1.02), 4)  # Simulate price between 0.98 and 1.02
        logging.info(f"Simulated Binance current price: {simulated_price}")
        update_price_history(price_history_binance, simulated_price)
        evaluate_market_and_execute_orders(simulated_price, price_history_binance, "Binance")
        time.sleep(10)  # Simulate a price update every 10 seconds

# Main execution function
def main():
    # Log simulation mode
    logging.info(f"Simulation Mode: {SIMULATION_MODE}")

    # Send startup message
    send_startup_message()

    # Fetch and store the minimum quantity and step size for Binance symbol
    symbol_info = get_binance_symbol_info(symbol_binance)
    if symbol_info:
        lot_size_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'), None)
        if lot_size_filter:
            global MIN_QUANTITY, STEP_SIZE
            MIN_QUANTITY = float(lot_size_filter['minQty'])  # Float for precise quantity
            STEP_SIZE = float(lot_size_filter['stepSize'])
            logging.info(f"Binance LOT_SIZE for {symbol_binance} - minQty: {MIN_QUANTITY}, stepSize: {STEP_SIZE}")
        else:
            logging.error(f"LOT_SIZE filter not found for {symbol_binance}.")
            MIN_QUANTITY = 1.0  # Default to 1.0 if not found
            STEP_SIZE = 1.0       # Default to 1.0 if not found
    else:
        MIN_QUANTITY = 1.0  # Fallback value
        STEP_SIZE = 1.0       # Fallback value

    # Start periodic updates in a separate thread
    global periodic_thread
    periodic_thread = threading.Thread(target=periodic_updates, daemon=True)
    periodic_thread.start()

    # Start Monitoring Thread in a separate thread
    monitoring_thread = threading.Thread(target=monitor_orders, daemon=True)
    monitoring_thread.start()

    if enable_binance:
        if not SIMULATION_MODE:
            # Start WebSocket to monitor the market if enabled and not in simulation mode
            threading.Thread(target=start_ws_binance, daemon=True).start()

            # Create and maintain User Data WebSocket
            listen_key_binance = create_listen_key_binance()
            if listen_key_binance:
                threading.Thread(target=maintain_listen_key_binance, args=(listen_key_binance,), daemon=True).start()
                threading.Thread(target=start_ws_binance_user_data, args=(listen_key_binance,), daemon=True).start()
        else:
            # Start simulating price updates
            logging.info("Starting simulated price updates.")
            threading.Thread(target=simulate_price_update, daemon=True).start()

    if enable_kraken and not SIMULATION_MODE:
        threading.Thread(target=start_ws_kraken, daemon=True).start()

    # Optionally, initiate the first BUY order if no orders are present and in simulation mode
    if SIMULATION_MODE:
        with open_orders_lock:
            if not any(open_orders["Binance"][side]["orderId"] for side in ["BUY", "SELL"]):
                place_buy_order_binance()

    # Keep the main thread running
    while not terminate_flag.is_set():
        time.sleep(1)

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        logging.error(f"Unhandled exception: {e}", exc_info=True)
        send_telegram_message(f"Bot crashed due to an unhandled exception: {e}")
        graceful_exit(None, None)
