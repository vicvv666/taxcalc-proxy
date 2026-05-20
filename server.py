"""
TaxCalc API Proxy + Admin Panel + Membership Manager
Deployed on Render.com
- Proxies all /api/* to CF Worker (bypasses workers.dev blocking)
- Adds /admin/* endpoints for membership management
- Uses JSON file for admin data (users, payments)
"""
import os
import json
import time
import requests
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__)

CF_WORKER_URL = 'https://taxcalc-api.vichoo2020.workers.dev'
ADMIN_PASS = os.environ.get('ADMIN_PASS', 'taxcalc2025admin')
DATA_FILE = '/tmp/taxcalc_admin.json'

# ============ Admin Data Store ============
def load_data():
    try:
        with open(DATA_FILE, 'r') as f:
            return json.load(f)
    except:
        return {'users': {}, 'payments': []}

def save_data(data):
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_membership(username):
    data = load_data()
    u = data['users'].get(username, {})
    mem = u.get('membership', 'free')
    exp = u.get('expires_at')
    if mem == 'pro' and exp:
        try:
            if time.time() > exp / 1000 if exp > 1e12 else exp:
                mem = 'free'  # expired
        except:
            pass
    return mem, u.get('expires_at'), u.get('payments', [])

def set_membership(username, membership, duration_days):
    data = load_data()
    if username not in data['users']:
        data['users'][username] = {'membership': 'free', 'expires_at': None, 'payments': []}
    u = data['users'][username]
    u['membership'] = membership
    if membership == 'pro' and duration_days > 0:
        exp = time.time() + duration_days * 86400
        u['expires_at'] = exp
    elif membership == 'free':
        u['expires_at'] = None
    save_data(data)
    return u

def add_payment(username, plan, method, amount, note=''):
    data = load_data()
    if username not in data['users']:
        data['users'][username] = {'membership': 'free', 'expires_at': None, 'payments': []}
    payment = {
        'date': time.strftime('%Y-%m-%d %H:%M'),
        'plan': plan,
        'method': method,
        'amount': amount,
        'note': note
    }
    data['users'][username]['payments'].append(payment)
    data['payments'].append({'username': username, **payment})
    save_data(data)
    return payment

# ============ CORS Helper ============
def cors_resp(data, status=200):
    resp = jsonify(data)
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    return resp, status

# ============ Proxy to CF Worker ============
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
        # If this is /api/user or /api/login, enrich with admin membership data
        try:
            if resp.status_code == 200 and '/api/user' in subpath:
                rjson = resp.json()
                if rjson.get('user') and rjson['user'].get('username'):
                    uname = rjson['user']['username']
                    mem, exp, pay = get_membership(uname)
                    # Admin data takes priority over CF Worker data
                    if mem != 'free' or rjson['user'].get('membership') == 'free':
                        rjson['user']['membership'] = mem
                        rjson['user']['expires_at'] = exp
                        rjson['admin_payments'] = pay
                    response = app.response_class(
                        response=json.dumps(rjson, ensure_ascii=False),
                        status=200,
                        content_type='application/json',
                    )
                    response.headers['Access-Control-Allow-Origin'] = '*'
                    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
                    return response
        except:
            pass

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
    data = load_data()
    users = []
    for uname, udata in data['users'].items():
        mem, exp, _ = get_membership(uname)
        users.append({
            'username': uname,
            'membership': mem,
            'expires_at': exp,
            'payments_count': len(udata.get('payments', []))
        })
    return cors_resp({'ok': True, 'users': users, 'total_payments': len(data['payments'])})

@app.route('/admin/user/<username>', methods=['GET', 'OPTIONS'])
def admin_get_user(username):
    if not check_admin():
        return cors_resp({'error': 'Unauthorized'}, 401)
    data = load_data()
    u = data['users'].get(username)
    if not u:
        # Also try CF Worker
        try:
            # Can't lookup without token, return what we have
            return cors_resp({'ok': True, 'user': {'username': username, 'membership': 'free', 'expires_at': None, 'payments': []}})
        except:
            pass
    mem, exp, pay = get_membership(username)
    return cors_resp({'ok': True, 'user': {'username': username, 'membership': mem, 'expires_at': exp, 'payments': pay}})

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
    return cors_resp({'ok': True, 'user': {'username': username, 'membership': u['membership'], 'expires_at': u['expires_at']}})

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
    data = load_data()
    pro_count = sum(1 for u in data['users'].values() if u.get('membership') == 'pro')
    return cors_resp({
        'ok': True,
        'total_users': len(data['users']),
        'pro_users': pro_count,
        'free_users': len(data['users']) - pro_count,
        'total_payments': len(data['payments'])
    })

# ============ Admin Panel ============
@app.route('/admin')
def admin_panel():
    return send_from_directory('templates', 'admin.html')

@app.route('/health')
def health():
    return jsonify({'ok': True, 'service': '税算寶 API Proxy + Admin', 'target': CF_WORKER_URL})

if __name__ == '__main__':
    # Initialize data file
    load_data()
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
