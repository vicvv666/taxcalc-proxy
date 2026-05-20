"""
TaxCalc API Proxy + Admin Panel + Membership Manager
Deployed on Render.com
- Proxies all /api/* to CF Worker (bypasses workers.dev blocking)
- Intercepts register/login to auto-capture all users
- Adds /admin/* endpoints for membership management
- Uses SQLite for persistent storage (survives Render restarts)
"""
import os
import json
import time
import sqlite3
import requests
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__)

CF_WORKER_URL = 'https://taxcalc-api.vichoo2020.workers.dev'
ADMIN_PASS = os.environ.get('ADMIN_PASS', 'taxcalc2025admin')

# Use persistent storage on Render (/data is persistent on paid, /opt/render/project/src for free)
# For free tier, use project src dir
DATA_DIR = os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__)))
DB_FILE = os.path.join(DATA_DIR, 'taxcalc_admin.db')

# ============ SQLite Database ============
def get_db():
    db = sqlite3.connect(DB_FILE)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    return db

def init_db():
    db = get_db()
    db.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            user_id INTEGER,
            membership TEXT DEFAULT 'free',
            expires_at REAL,
            first_seen TEXT,
            last_seen TEXT,
            source TEXT DEFAULT 'auto'
        );
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            date TEXT,
            plan TEXT,
            method TEXT,
            amount TEXT,
            note TEXT
        );
        CREATE TABLE IF NOT EXISTS sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT,
            timestamp TEXT,
            detail TEXT
        );
    ''')
    db.commit()
    db.close()

def upsert_user(username, user_id=None, membership=None, expires_at=None, source='auto'):
    """Insert or update a user. Called on every register/login via proxy."""
    db = get_db()
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    existing = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    if existing:
        updates = ['last_seen=?']
        vals = [now]
        if user_id: updates.append('user_id=?'); vals.append(user_id)
        if membership and not existing['membership'] == 'pro': 
            # Don't downgrade pro users from auto-capture
            if membership != 'free':
                updates.append('membership=?'); vals.append(membership)
        if expires_at is not None:
            updates.append('expires_at=?'); vals.append(expires_at)
        vals.append(username)
        db.execute(f"UPDATE users SET {','.join(updates)} WHERE username=?", vals)
    else:
        db.execute(
            'INSERT INTO users (username, user_id, membership, expires_at, first_seen, last_seen, source) VALUES (?,?,?,?,?,?,?)',
            (username, user_id, membership or 'free', expires_at, now, now, source)
        )
    db.commit()
    db.close()

def get_user(username):
    db = get_db()
    u = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    db.close()
    if not u:
        return None
    mem = u['membership']
    exp = u['expires_at']
    if mem == 'pro' and exp and time.time() > exp:
        mem = 'free'
    payments = get_payments(username)
    return {
        'username': u['username'],
        'user_id': u['user_id'],
        'membership': mem,
        'expires_at': u['expires_at'],
        'first_seen': u['first_seen'],
        'last_seen': u['last_seen'],
        'source': u['source'],
        'payments': payments
    }

def get_payments(username):
    db = get_db()
    rows = db.execute('SELECT * FROM payments WHERE username=? ORDER BY date DESC', (username,)).fetchall()
    db.close()
    return [dict(r) for r in rows]

def set_membership(username, membership, duration_days):
    db = get_db()
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    existing = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    if not existing:
        db.execute(
            'INSERT INTO users (username, membership, expires_at, first_seen, last_seen, source) VALUES (?,?,?,?,?,?)',
            (username, membership, None, now, now, 'admin')
        )
        existing = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    
    exp = None
    if membership == 'pro' and duration_days > 0:
        exp = time.time() + duration_days * 86400
    
    db.execute('UPDATE users SET membership=?, expires_at=?, last_seen=? WHERE username=?',
               (membership, exp, now, username))
    db.commit()
    db.close()
    return {'username': username, 'membership': membership, 'expires_at': exp}

def add_payment(username, plan, method, amount, note=''):
    db = get_db()
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    # Ensure user exists
    existing = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    if not existing:
        db.execute(
            'INSERT INTO users (username, membership, expires_at, first_seen, last_seen, source) VALUES (?,?,?,?,?,?)',
            (username, 'free', None, now, now, 'admin')
        )
    db.execute(
        'INSERT INTO payments (username, date, plan, method, amount, note) VALUES (?,?,?,?,?,?)',
        (username, now, plan, method, amount, note)
    )
    db.commit()
    pay_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
    db.close()
    return {'id': pay_id, 'username': username, 'date': now, 'plan': plan, 'method': method, 'amount': amount, 'note': note}

def list_users(page=1, per_page=50, filter_mem=None):
    db = get_db()
    offset = (page - 1) * per_page
    if filter_mem:
        total = db.execute('SELECT COUNT(*) FROM users WHERE membership=?', (filter_mem,)).fetchone()[0]
        rows = db.execute('SELECT * FROM users WHERE membership=? ORDER BY last_seen DESC LIMIT ? OFFSET ?',
                          (filter_mem, per_page, offset)).fetchall()
    else:
        total = db.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        rows = db.execute('SELECT * FROM users ORDER BY last_seen DESC LIMIT ? OFFSET ?',
                          (per_page, offset)).fetchall()
    db.close()
    users = []
    for r in rows:
        mem = r['membership']
        exp = r['expires_at']
        if mem == 'pro' and exp and time.time() > exp:
            mem = 'free'
        pay_count = len(get_payments(r['username']))
        users.append({
            'username': r['username'],
            'user_id': r['user_id'],
            'membership': mem,
            'expires_at': exp,
            'first_seen': r['first_seen'],
            'last_seen': r['last_seen'],
            'payments_count': pay_count,
            'source': r['source']
        })
    return {'total': total, 'page': page, 'per_page': per_page, 'users': users}

def get_stats():
    db = get_db()
    total = db.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    pro = db.execute("SELECT COUNT(*) FROM users WHERE membership='pro'").fetchone()[0]
    pro_active = 0
    pro_rows = db.execute("SELECT expires_at FROM users WHERE membership='pro'").fetchall()
    for r in pro_rows:
        if r['expires_at'] and time.time() <= r['expires_at']:
            pro_active += 1
    payments_total = db.execute('SELECT COUNT(*) FROM payments').fetchone()[0]
    recent = db.execute(
        "SELECT COUNT(*) FROM users WHERE last_seen >= datetime('now', '-7 days')"
    ).fetchone()[0]
    db.close()
    return {
        'total_users': total,
        'pro_users': pro,
        'pro_active': pro_active,
        'free_users': total - pro,
        'total_payments': payments_total,
        'active_7d': recent
    }

# ============ CORS Helper ============
def cors_resp(data, status=200):
    resp = jsonify(data)
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    return resp, status

# ============ Proxy to CF Worker (with user capture) ============
@app.route('/api/<path:subpath>', methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'])
@app.route('/api/', methods=['GET', 'OPTIONS'])
def proxy_api(subpath=''):
    if request.method == 'OPTIONS':
        return cors_resp({'ok': True})

    target_url = f"{CF_WORKER_URL}/api/{subpath}"
    if request.query_string:
        target_url += f"?{request.query_string.decode()}"

    fwd_headers = {
        'Content-Type': request.headers.get('Content-Type', 'application/json'),
        'Accept': 'application/json',
    }
    auth = request.headers.get('Authorization')
    if auth:
        fwd_headers['Authorization'] = auth

    body = None
    if request.method in ('POST', 'PUT'):
        body = request.get_data()

    try:
        resp = requests.request(
            method=request.method,
            url=target_url,
            headers=fwd_headers,
            data=body,
            timeout=15,
        )
        
        # Auto-capture users from register/login responses
        try:
            if resp.status_code == 200:
                rjson = resp.json()
                # Capture from register
                if subpath == 'register' and rjson.get('ok') and rjson.get('user'):
                    u = rjson['user']
                    upsert_user(
                        u['username'],
                        u.get('id'),
                        u.get('membership', 'free'),
                        u.get('expires_at'),
                        'register'
                    )
                # Capture from login
                elif subpath == 'login' and rjson.get('ok') and rjson.get('user'):
                    u = rjson['user']
                    upsert_user(
                        u['username'],
                        u.get('id'),
                        u.get('membership', 'free'),
                        u.get('expires_at'),
                        'login'
                    )
                # Enrich /api/user with admin membership data
                elif subpath == 'user' and rjson.get('user') and rjson.get('user', {}).get('username'):
                    uname = rjson['user']['username']
                    admin_user = get_user(uname)
                    if admin_user and admin_user['membership'] != 'free':
                        rjson['user']['membership'] = admin_user['membership']
                        rjson['user']['expires_at'] = admin_user['expires_at']
                        rjson['admin_payments'] = admin_user['payments']
                    # Also capture/update user
                    upsert_user(uname, rjson['user'].get('id'), rjson['user'].get('membership'), rjson['user'].get('expires_at'), 'api_user')
                    response = app.response_class(
                        response=json.dumps(rjson, ensure_ascii=False),
                        status=200,
                        content_type='application/json',
                    )
                    response.headers['Access-Control-Allow-Origin'] = '*'
                    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
                    return response
        except Exception as e:
            app.logger.warning(f'User capture error: {e}')

        response = app.response_class(
            response=resp.content,
            status=resp.status_code,
            content_type=resp.headers.get('Content-Type', 'application/json'),
        )
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        return response
    except Exception as e:
        return cors_resp({'error': f'Proxy error: {str(e)}'}, 502)

# ============ Admin Auth ============
def check_admin():
    auth = request.headers.get('Authorization', '')
    if auth == f'Bearer {ADMIN_PASS}':
        return True
    if request.args.get('admin') == ADMIN_PASS:
        return True
    return False

# ============ Admin Endpoints ============
@app.route('/admin/login', methods=['POST', 'OPTIONS'])
def admin_login():
    if request.method == 'OPTIONS':
        return cors_resp({'ok': True})
    data = request.get_json(silent=True) or {}
    if data.get('password') == ADMIN_PASS:
        return cors_resp({'ok': True, 'token': ADMIN_PASS})
    return cors_resp({'error': '密碼錯誤'}, 401)

@app.route('/admin/users', methods=['GET', 'OPTIONS'])
def admin_list_users():
    if not check_admin():
        return cors_resp({'error': 'Unauthorized'}, 401)
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 50))
    filter_mem = request.args.get('membership', None)
    result = list_users(page, per_page, filter_mem)
    stats = get_stats()
    return cors_resp({'ok': True, **result, **stats})

@app.route('/admin/user/<username>', methods=['GET', 'OPTIONS'])
def admin_get_user(username):
    if not check_admin():
        return cors_resp({'error': 'Unauthorized'}, 401)
    u = get_user(username)
    if not u:
        return cors_resp({'ok': True, 'user': {'username': username, 'membership': 'free', 'expires_at': None, 'payments': []}})
    return cors_resp({'ok': True, 'user': u})

@app.route('/admin/user/<username>/membership', methods=['PUT', 'OPTIONS'])
def admin_set_membership(username):
    if not check_admin():
        return cors_resp({'error': 'Unauthorized'}, 401)
    if request.method == 'OPTIONS':
        return cors_resp({'ok': True})
    data = request.get_json(silent=True) or {}
    membership = data.get('membership', 'free')
    duration_days = data.get('duration_days', 30)
    u = set_membership(username, membership, duration_days)
    return cors_resp({'ok': True, 'user': u})

@app.route('/admin/user/<username>/payment', methods=['POST', 'OPTIONS'])
def admin_add_payment(username):
    if not check_admin():
        return cors_resp({'error': 'Unauthorized'}, 401)
    if request.method == 'OPTIONS':
        return cors_resp({'ok': True})
    data = request.get_json(silent=True) or {}
    p = add_payment(
        username,
        data.get('plan', 'manual'),
        data.get('method', 'manual'),
        data.get('amount', ''),
        data.get('note', '')
    )
    return cors_resp({'ok': True, 'payment': p})

@app.route('/admin/stats', methods=['GET'])
def admin_stats():
    if not check_admin():
        return cors_resp({'error': 'Unauthorized'}, 401)
    return cors_resp({'ok': True, **get_stats()})

@app.route('/admin/payments', methods=['GET'])
def admin_all_payments():
    if not check_admin():
        return cors_resp({'error': 'Unauthorized'}, 401)
    db = get_db()
    rows = db.execute('SELECT * FROM payments ORDER BY date DESC LIMIT 100').fetchall()
    db.close()
    return cors_resp({'ok': True, 'payments': [dict(r) for r in rows]})

# ============ Admin Panel ============
@app.route('/admin')
def admin_panel():
    return send_from_directory('templates', 'admin.html')

@app.route('/health')
def health():
    return jsonify({'ok': True, 'service': '税算寶 API Proxy + Admin', 'target': CF_WORKER_URL})

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
