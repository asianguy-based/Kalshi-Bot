import os
import sqlite3
import logging
from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_bcrypt import Bcrypt
from cryptography.fernet import Fernet
from bot_engine import bot_instance, log_buffer

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev_key')
ENCRYPTION_KEY = os.environ.get('ENCRYPTION_KEY')
cipher = Fernet(ENCRYPTION_KEY.encode()) if ENCRYPTION_KEY else None

bcrypt = Bcrypt(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

DB_NAME = "/app/bot_data.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT, password TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)''')
    
    # Default Admin
    c.execute("SELECT * FROM users WHERE username = ?", ('admin',))
    if not c.fetchone():
        hashed_pw = bcrypt.generate_password_hash('admin123').decode('utf-8')
        c.execute("INSERT INTO users (username, password) VALUES (?, ?)", ('admin', hashed_pw))
    conn.commit()
    conn.close()

class User(UserMixin):
    def __init__(self, id, username):
        self.id = id
        self.username = username

@login_manager.user_loader
def load_user(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    u = c.fetchone()
    conn.close()
    return User(id=u[0], username=u[1]) if u else None

# --- Routes ---

@app.route('/', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated: return redirect(url_for('dashboard'))
    if request.method == 'POST':
        data = request.get_json()
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE username = ?", (data['username'],))
        user_row = c.fetchone()
        conn.close()
        if user_row and bcrypt.check_password_hash(user_row[2], data['password']):
            login_user(User(id=user_row[0], username=user_row[1]))
            return jsonify({"status": "success", "redirect": "/dashboard"})
        return jsonify({"status": "error"}), 401
    return render_template('login.html')

@app.route('/dashboard')
@login_required
def dashboard(): return render_template('dashboard.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/api/config', methods=['GET', 'POST'])
@login_required
def handle_config():
    conn = sqlite3.connect(DB_NAME, timeout=10)
    c = conn.cursor()
    if request.method == 'POST':
        data = request.get_json()
        keys = ['kalshi_key_id', 'kalshi_private_key', 'target_market', 'market_keywords', 'min_profit_pct', 'max_bet_amount', 'min_liquidity', 'max_exposure', 'dry_run_mode']
        
        for k in keys:
            if k in data and data[k]:
                val = data[k]
                if 'key' in k and cipher: # Encrypt credentials
                    val = cipher.encrypt(val.encode()).decode()
                c.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (k, val))
        conn.commit()
        conn.close()
        return jsonify({"message": "Configuration Saved"})
    else:
        c.execute("SELECT key, value FROM config")
        rows = c.fetchall()
        safe_data = {}
        for k, v in rows:
            safe_data[k] = "********" if 'kalshi_key' in k or 'private_key' in k else v
        conn.close()
        return jsonify(safe_data)

@app.route('/api/update_password', methods=['POST'])
@login_required
def update_password():
    try:
        data = request.get_json()
        new_pass = data.get('new_password')
        hashed_pw = bcrypt.generate_password_hash(new_pass).decode('utf-8')
        conn = sqlite3.connect(DB_NAME, timeout=10)
        c = conn.cursor()
        c.execute("UPDATE users SET password = ? WHERE id = ?", (hashed_pw, current_user.id))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/bot/logs')
@login_required
def get_logs():
    return jsonify({"logs": list(log_buffer.buffer)})

@app.route('/api/bot/trade', methods=['POST']) # FIXED: Added @app.route
@login_required
def trigger_trade():
    data = request.get_json()
    ticker = data.get('ticker')
    if not ticker:
        return jsonify({"status": "error", "message": "No ticker provided"}), 400
    
    # Call the bot engine
    result = bot_instance.execute_trade(ticker)
    return jsonify(result)

@app.route('/api/bot/<action>', methods=['POST', 'GET'])
@login_required
def bot_ctrl(action):
    if action == 'status': 
        return jsonify({"status": "RUNNING" if bot_instance.is_running else "IDLE"})
    if request.method == 'POST':
        if action == 'start': return jsonify({"message": bot_instance.start_bot()})
        if action == 'stop': return jsonify({"message": bot_instance.stop_bot()})
    return jsonify({"error": "Invalid"}), 400

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000)
