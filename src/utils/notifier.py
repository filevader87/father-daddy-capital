import os
import requests
from typing import Optional
from src.utils.logger import get_logger

logger = get_logger(__name__)

def send_telegram_message(message: str, chat_id: Optional[str] = None) -> bool:
    """Send a message via Telegram bot.
    
    Args:
        message (str): The message to send
        chat_id (str, optional): The chat ID to send to. Defaults to env var.
        
    Returns:
        bool: True if message was sent successfully, False otherwise
    """
    try:
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not bot_token:
            logger.warning("TELEGRAM_BOT_TOKEN not set")
            return False
            
        chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
        if not chat_id:
            logger.warning("TELEGRAM_CHAT_ID not set")
            return False
            
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        data = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML"
        }
        
        response = requests.post(url, json=data)
        if response.status_code == 200:
            logger.info(f"Message sent to Telegram: {message[:50]}...")
            return True
        else:
            logger.error(f"Failed to send message to Telegram: {response.text}")
            return False
            
    except Exception as e:
        logger.error(f"Error sending message to Telegram: {e}")
        return False 