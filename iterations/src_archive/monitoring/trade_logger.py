
import sqlite3
import datetime

class TradeLogger:
    def __init__(self, db_name="trades.db"):
        self.conn = sqlite3.connect(db_name)
        self.cursor = self.conn.cursor()
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                side TEXT,
                quantity REAL,
                price REAL,
                exchange TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        self.conn.commit()

    def log_trade(self, symbol, side, quantity, price, exchange):
        """Logs executed trades into a database for performance tracking."""
        self.cursor.execute('''
            INSERT INTO trades (symbol, side, quantity, price, exchange) 
            VALUES (?, ?, ?, ?, ?)''', (symbol, side, quantity, price, exchange))
        self.conn.commit()

    def get_trade_history(self, symbol=None):
        """Fetches trade history for a specific symbol or all trades."""
        query = "SELECT * FROM trades" if symbol is None else "SELECT * FROM trades WHERE symbol = ?"
        self.cursor.execute(query, (symbol,) if symbol else ())
        return self.cursor.fetchall()
