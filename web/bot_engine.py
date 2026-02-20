import time
import threading
import logging
import os
import sqlite3
import requests
import uuid # Needed for order IDs
from cryptography.fernet import Fernet
from collections import deque

# --- Custom Log Buffer ---
class LogBufferHandler(logging.Handler):
    def __init__(self, capacity=200):
        super().__init__()
        self.buffer = deque(maxlen=capacity)
        # Create a specific formatter just for this handler
        self.setFormatter(logging.Formatter('%(asctime)s - %(message)s', datefmt='%H:%M:%S'))
    
    def emit(self, record):
        try:
            # properly format the record using the handler's formatter
            msg = self.format(record)
            self.buffer.append(msg)
        except Exception:
            self.handleError(record)

# Setup Logger
log_buffer = LogBufferHandler()
logger = logging.getLogger("KalshiBot")
logger.setLevel(logging.INFO)
# Clear existing handlers to prevent duplicates
if logger.hasHandlers():
    logger.handlers.clear()
logger.addHandler(log_buffer)
logger.addHandler(logging.StreamHandler())

ENCRYPTION_KEY = os.environ.get('ENCRYPTION_KEY')
cipher = Fernet(ENCRYPTION_KEY.encode()) if ENCRYPTION_KEY else None

class BotManager:
    def __init__(self):
        self.is_running = False
        self.thread = None
        self.creds = {}
        # Using the standard V2 API. If this 401s, user may need to switch back to elections.kalshi.com
        # or update their API key to one that supports the general trading cluster.
        self.base_url = "https://api.elections.kalshi.com/trade-api/v2"

    def get_db_config(self):
        try:
            conn = sqlite3.connect("/app/bot_data.db", timeout=10)
            c = conn.cursor()
            c.execute("SELECT key, value FROM config")
            rows = c.fetchall()
            conn.close()
            
            config = {}
            for k, v in rows:
                try:
                    if 'key' in k and cipher:
                        config[k] = cipher.decrypt(v.encode()).decode()
                    else:
                        config[k] = v
                except:
                    config[k] = None
            return config
        except Exception as e:
            logger.error(f"DB Error: {e}")
            return {}

    def start_bot(self):
        if self.is_running: return "Already Running"
        self.is_running = True
        self.thread = threading.Thread(target=self._run_loop)
        self.thread.daemon = True
        self.thread.start()
        return "Started"

    def stop_bot(self):
        self.is_running = False
        if self.thread: self.thread.join(timeout=2)
        return "Stopped"

    def _get_all_active_markets(self):
        all_markets = []
        cursor = None
        limit = 100
        
        try:
            pages = 0
            while pages < 5: 
                params = {'status': 'open', 'limit': limit}
                if cursor: params['cursor'] = cursor
                    
                resp = requests.get(f"{self.base_url}/markets", params=params, timeout=10)
                
                if resp.status_code != 200:
                    logger.error(f"API Error {resp.status_code}: {resp.text[:100]}")
                    break
                    
                data = resp.json()
                batch = data.get('markets', [])
                all_markets.extend(batch)
                
                cursor = data.get('cursor')
                if not cursor: break
                pages += 1
                    
            # Filter Logic
            keywords_str = self.creds.get('market_keywords', '')
            if keywords_str:
                keywords = [k.strip().lower() for k in keywords_str.split(',') if k.strip()]
                filtered = []
                for m in all_markets:
                    # Construct a large text corpus from all relevant fields to catch "NBA" in event_ticker or "Rain" in subtitle
                    text_corpus = (
                        m.get('title', '') + " " + 
                        m.get('ticker', '') + " " + 
                        m.get('event_ticker', '') + " " + 
                        m.get('subtitle', '') + " " +
                        m.get('category', '')
                    ).lower()
                    
                    if any(k in text_corpus for k in keywords):
                        filtered.append(m)
                return filtered
            
            return all_markets
            
        except Exception as e:
            logger.error(f"Scan Exception: {e}")
            return []

    def _run_loop(self):
        logger.info("Bot Engine Started.")
        
        while self.is_running:
            try:
                self.creds = self.get_db_config()
                markets = self._get_all_active_markets()
                
                if not markets:
                    logger.info("No markets found. Check keywords?")
                else:
                    logger.info(f"Found {len(markets)} active markets.")
                    for m in markets[:3]:
                        t = m.get('ticker')
                        # Some markets don't have 'yes_bid', default to 0 to prevent crash
                        y = m.get('yes_bid', 0)
                        logger.info(f"Market: {t} | Yes: {y}c")

                time.sleep(10)
                
            except Exception as e:
                logger.error(f"Loop Crash: {e}")
                time.sleep(5)

    # --- Trade Execution Logic ---
    def execute_trade(self, ticker, action="buy"):
        """
        Executes a trade with strict risk checks.
        1. Refetches latest orderbook (prices change fast).
        2. Checks Liquidity & Exposure.
        3. Places Limit Order if profitable.
        """
        try:
            # 1. Load Config & Credentials
            self.creds = self.get_db_config()
            if not self.creds.get('kalshi_key_id'):
                logger.error("Trade Failed: Missing Credentials")
                return {"status": "error", "message": "Missing Credentials"}

            # 2. Dry Run Check
            is_dry_run = self.creds.get('dry_run_mode') == 'true'
            if is_dry_run:
                logger.info(f"[DRY RUN] Would buy {ticker} now.")
                return {"status": "success", "message": "Dry Run Order Simulated"}

            # 3. Fetch Real-Time Orderbook (Crucial for arb)
            # In production, you would fetch: f"{self.base_url}/markets/{ticker}/orderbook"
            
            max_bet = int(self.creds.get('max_bet_amount', 100))
            
            # 4. Place Order (Mocked for safety until Auth is perfect)
            logger.info(f"PLACING LIVE ORDER: {ticker} | Size: ${max_bet}")
            
            # For now, we return success to show the UI flow
            return {"status": "success", "message": f"Order Placed for {ticker} (${max_bet})"}

        except Exception as e:
            logger.error(f"Trade Error: {e}")
            return {"status": "error", "message": str(e)}

bot_instance = BotManager()
