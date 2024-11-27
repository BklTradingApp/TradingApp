import os
import requests
import hmac
import hashlib
from dotenv import load_dotenv
from datetime import datetime, timedelta
import time
import logging
import pandas as pd
import signal
import websocket
import json
import numpy as np  # Added for RSI calculations
from requests.exceptions import Timeout, ConnectionError  # Added specific exceptions for handling network errors

# Setup logging to record activity
logging.basicConfig(filename='trading_bot.log', level=logging.WARNING, format='%(asctime)s - %(message)s')

# Load environment variables from .env file
load_dotenv()

# Get API keys and credentials from environment variables
api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_SECRET_KEY")
telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")

# Base URL for Binance.US
base_url = "https://api.binance.us"

# Configuration
BUY_THRESHOLD = 0.9982
SELL_THRESHOLD = 1.0025
ORDER_TIMEOUT = 10  # Minutes
POSITION_SIZE_PERCENTAGE = 0.05  # Use 50% of available balance for initial testing
MAX_RETRY = 3
SIMULATION_MODE = True  # Set to False for real trading
RSI_PERIOD = 14  # RSI period for calculations
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
RATE_LIMIT_INTERVAL = 1  # Minimum interval between API calls in seconds

# For WebSocket URL
ws_url = "wss://stream.binance.us:9443/ws/usdtusd@ticker"

# Order history dataframe
order_history = pd.DataFrame(columns=['Order ID', 'Side', 'Quantity', 'Price', 'Timestamp', 'Status'])
order_history_file = 'order_history.csv'

# Price history for RSI calculation
price_history = []

# Rate limit tracking
LAST_API_CALL = 0

# Graceful shutdown handler
def graceful_exit(signum, frame):
    logging.info("Received shutdown signal. Saving order history and exiting gracefully...")
    if not order_history.empty:
        order_history.to_csv(order_history_file, index=False)
    exit(0)

signal.signal(signal.SIGINT, graceful_exit)
signal.signal(signal.SIGTERM, graceful_exit)

# Function to send Telegram message
def send_telegram_message(message):
    try:
        url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": telegram_chat_id,
            "text": message
        }
        response = requests.post(url, json=payload)
        if response.status_code == 200:
            logging.info(f"Telegram message sent: {message}")
        else:
            logging.error(f"Failed to send Telegram message. Status Code: {response.status_code}. Response: {response.text}")
    except Exception as e:
        logging.error(f"Failed to send Telegram message: {e}")

# Function to make requests with retry logic
def make_request_with_retries(url, headers, params, method="get"):
    global LAST_API_CALL
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
                response = requests.post(url, headers=headers, params=params, timeout=10, verify=True)
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
                time.sleep(5)  # Backoff before retrying
        except (Timeout, ConnectionError) as e:
            logging.error(f"Network error on attempt {attempt + 1}: {e}")
            time.sleep(5)  # Backoff before retrying
    return None

# Function to get account balance
def get_account_balance(asset):
    endpoint = "/api/v3/account"
    timestamp = int(datetime.now().timestamp() * 1000)
    params = {"timestamp": timestamp}
    query_string = "&".join([f"{k}={v}" for k, v in params.items()])
    signature = hmac.new(api_secret.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    headers = {"X-MBX-APIKEY": api_key}
    params["signature"] = signature

    response = make_request_with_retries(f"{base_url}{endpoint}", headers, params)
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

# Function to calculate RSI
def calculate_rsi(prices, period=RSI_PERIOD):
    if len(prices) < period + 1:
        return None  # Not enough data to calculate RSI
    deltas = np.diff(prices)
    seed = deltas[:period]
    up = seed[seed >= 0].sum() / period
    down = -seed[seed < 0].sum() / period
    rs = up / down if down != 0 else 0
    rsi = 100 - (100 / (1 + rs))

    for delta in deltas[period:]:
        if delta > 0:
            upval = delta
            downval = 0
        else:
            upval = 0
            downval = -delta

        up = (up * (period - 1) + upval) / period
        down = (down * (period - 1) + downval) / period
        rs = up / down if down != 0 else 0
        rsi = 100 - (100 / (1 + rs))

    return rsi

# Function to determine if order should be placed
def should_place_order(current_price, side):
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

# Function to place a limit order
def place_limit_order(symbol, side, quantity, price):
    if SIMULATION_MODE:
        logging.info(f"Simulating order placement: {side} {quantity:.2f} of {symbol} at ${price:.4f}")
        send_telegram_message(f"Simulating order placement: {side} {quantity:.2f} of {symbol} at ${price:.4f}")
        return None, None

    if not should_place_order(price, side):
        logging.info(f"Conditions not met for placing {side} order at ${price:.4f}")
        return None, None

    endpoint = "/api/v3/order"
    timestamp = int(datetime.now().timestamp() * 1000)

    params = {
        "symbol": symbol,
        "side": side,
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": "{:.2f}".format(quantity),
        "price": "{:.4f}".format(price),
        "timestamp": timestamp
    }
    query_string = "&".join([f"{k}={v}" for k, v in params.items()])
    signature = hmac.new(api_secret.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    params["signature"] = signature
    headers = {"X-MBX-APIKEY": api_key}

    response = make_request_with_retries(f"{base_url}{endpoint}", headers, params, method="post")
    if response:
        try:
            order_response = response.json()
            logging.info(f"Order placed successfully: {order_response}")
            send_telegram_message(f"Order placed successfully: {order_response}")
            order_history_entry = {
                'Order ID': order_response['orderId'],
                'Side': side,
                'Quantity': quantity,
                'Price': price,
                'Timestamp': datetime.now(),
                'Status': 'NEW'
            }
            add_order_to_history(order_history_entry)
            return order_response['orderId'], datetime.now()
        except Exception as e:
            logging.error(f"An error occurred while processing the order response: {e}")
    return None, None

# Add order details to history and save it
def add_order_to_history(order):
    global order_history
    order_history = order_history.append(order, ignore_index=True)
    order_history.to_csv(order_history_file, index=False)

# WebSocket logic to receive real-time price updates
def on_message(ws, message):
    data = json.loads(message)
    current_price = float(data['c'])
    price_history.append(current_price)  # Add current price to price history
    if len(price_history) > RSI_PERIOD + 1:
        price_history.pop(0)  # Maintain only the required number of prices for RSI calculation

    # Check account balance before proceeding
    usd_balance = get_account_balance('USD')
    usdt_balance = get_account_balance('USDT')
    total_balance = usd_balance + usdt_balance

    if total_balance < 2750:
        logging.warning("Total account balance is below $2750. Stopping the bot.")
        send_telegram_message("Total account balance is below $2750. Stopping the bot.")
        graceful_exit(None, None)

    process_price_update(current_price)

def on_error(ws, error):
    logging.error(f"WebSocket error: {error}")
    send_telegram_message(f"WebSocket error: {error}")

def on_close(ws, close_status_code, close_msg):
    logging.warning(f"WebSocket closed: {close_status_code} - {close_msg}")
    send_telegram_message(f"WebSocket closed. Attempting to reconnect in 10 seconds...")
    time.sleep(10)
    start_ws()  # Reconnect after 10 seconds

def process_price_update(current_price):
    logging.info(f"Current price: {current_price}")
    # Add the logic for buy/sell decision based on current price here

# Function to send periodic updates to Telegram
def send_periodic_update():
    try:
        usd_balance = get_account_balance('USD')
        usdt_balance = get_account_balance('USDT')
        current_price = price_history[-1] if price_history else 'N/A'

        # Get current order status
        if order_history.empty:
            order_status = "No orders placed."
        else:
            recent_orders = order_history[['Order ID', 'Side', 'Quantity', 'Price', 'Status']].tail().to_string(index=False)
            recent_orders = recent_orders.replace('\n', '\n  ')
            order_status = f"Recent Orders:\n  {recent_orders}\n"

        message = (f"Periodic Update:\n"
                   f"Account Balance:\n  USD: ${usd_balance:.2f}\n  USDT: {usdt_balance:.2f}\n"
                   f"Current USDT Price: {current_price}\n"
                   f"{order_status}")

        send_telegram_message(message)
    except Exception as e:
        logging.error(f"Failed to send periodic update: {e}")

# Start periodic update in a separate thread
import threading

def periodic_updates():
    while True:
        send_periodic_update()
        time.sleep(10800)  # Send update every 3 hours

threading.Thread(target=periodic_updates, daemon=True).start()

# Start WebSocket client
def start_ws():
    send_telegram_message("Trading bot started successfully and is now monitoring the market.")
    ws = websocket.WebSocketApp(ws_url, on_message=on_message, on_error=on_error, on_close=on_close)
    ws.run_forever()

# Start WebSocket to monitor the market
start_ws()
