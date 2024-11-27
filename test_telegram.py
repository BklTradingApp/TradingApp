import os
import requests
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Get API keys and credentials from environment variables
telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")

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
            print(f"Telegram message sent successfully: {message}")
        else:
            print(f"Failed to send Telegram message. Status Code: {response.status_code}. Response: {response.text}")
    except Exception as e:
        print(f"Failed to send Telegram message: {e}")

# Test sending a message
test_message = "Test message from my trading bot!"
send_telegram_message(test_message)
