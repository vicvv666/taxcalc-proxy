"""
TaxCalc API Proxy + Admin Panel + Membership Manager
Deployed on Render.com
- Proxies all /api/* to CF Worker (bypasses workers.dev blocking)
- Intercepts register/login to auto-capture all users
- Adds /admin/* endpoints for membership management
- Uses GitHub repo for persistent storage (survives Render restarts)
"""
import os
import json
import time
import requests as http_requests
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__)

CF_WORKER_URL = 'https://taxcalc-api.vichoo2020.workers.dev'
ADMIN_PASS = os.environ.get('ADMIN_PASS', 'taxcalc2025admin')

# GitHub persistence - store admin data in repo
GH_TOKEN = os.environ.get('GH_TOKEN', '')
GH_REPO = 'vicvv666/taxcalc-proxy'
GH_FILE = 'data/admin_data.json'
GH_API = f'https://api.github.com/repos/{GH_REPO}/contents/{GH_FILE}'

# ============ GitHub Data Store ============
def load_data():
    """Load admin data from GitHub repo"""
    try:
        if GH_TOKEN:
            r = http_requests.get(GH_API, headers={
                'Authorization': f'token {GH_TOKEN}',
                'Accept': 'application/vnd.github.v3+json'
            }, timeout=10)
            if r.status_code == 200:
                import base64
                content = base64.b64decode(r.json()['content']).decode()
                data = json.loads(content)
                data['_sha'] = r.json()['sha']
                return data
    except Exception as e:
        app.logger.warning(f'GitHub load error: {e}')
    # Fallback: local file
    try:
        with open('data/admin_data.json', 'r') as f:
            return json.load(f)
    except:
        return {'users': {}, 'payments': [], '_sha': ''}

def save_data(data):
    """Save admin data to GitHub repo"""
    save_data_local(data)
    if not GH_TOKEN:
        return
    sha = data.pop('_sha', '')
    import base64
    content = base64.b64encode(json.dumps(data, ensure_ascii=False, indent=2).encode()).decode()
    try:
        r = http_requests.put(GH_API, headers={
            'Authorization': f'token {GH_TOKEN}',
            'Accept': 'application/vnd.github.v3+json'
        }, json={
            'message': f'admin data update {time.strftime("%Y-%m-%d %H:%M")}',
            'content': content,
            'sha': sha
        }, timeout=15)
        if r.status_code == 200:
            data['_sha'] = r.json()['content']['sha']
    except Exception as e:
        app.logger.warning(f'GitHub save error: {e}')

def save_data_local(data):
    """Also save locally as backup"""
    try:
        os.makedirs('data', exist_ok=True)
        d = {k: v for k, v in data.items() if k != '_sha'}
        with open('data/admin_data.json', 'w') as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except:
        pass

# ============ User Management ============
def get_membership(username):
    data = load_data()
    u = data['users'].get(username, {})
    mem = u.get('membership', 'free')
    exp = u.get('expires_at')
    if mem == 'pro' and exp and time.time() > exp:
        mem = 'free'
    return mem, u.get('expires_at'), u.get('payments', [])

def upsert_user(username, user_id=None, membership=None, expires_at=None, source='auto'):
    data = load_data()
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    if username in data['users']:
        u = data['users'][username]
        u['last_seen'] = now
        if user_id: u['user_id'] = user_id
        # Don't downgrade pro from auto-capture
        if membership and membership != 'free' and u.get('membership') != 'pro':
            u['membership'] = membership
        if expires_at is not None and u.get('membership') == membership:
            u['expires_at'] = expires_at
    else:
        data['users'][username] = {
            'username': username,
            'user_id': user_id,
            'membership': membership or 'free',
            'expires_at': expires_at,
            'first_seen': now,
            'last_seen': now,
            'payments': [],
            'source': source
        }
    save_data(data)

def set_membership(username, membership, duration_days):
    data = load_data()
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    if username not in data['users']:
        data['users'][username] = {
            'username': username, 'membership': 'free', 'expires_at': None,
            'first_seen': now, 'last_seen': now, 'payments': [], 'source': 'admin'
        }
    u = data['users'][username]
    u['membership'] = membership
    u['last_seen'] = now
    if membership == 'pro' and duration_days > 0:
        u['expires_at'] = time.time() + duration_days * 86400
    elif membership == 'free':
        u['expires_at'] = None
    save_data(data)
    return {'username': username, 'membership': membership, 'expires_at': u['expires_at']}

def add_payment(username, plan, method, amount, note=''):
    data = load_data()
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    if username not in data['users']:
        data['users'][username] = {
            'username': username, 'membership': 'free', 'expires_at': None,
            'first_seen': now, 'last_seen': now, 'payments': [], 'source': 'admin'
        }
    payment = {'date': now, 'plan': plan, 'method': method, 'amount': amount, 'note': note}
    data['users'][username]['payments'].append(payment)
    data['payments'].append({'username': username, **payment})
    save_data(data)
    return payment

def list_users(page=1, per_page=50, filter_mem=None, search=None):
    data = load_data()
    now = time.time()
    all_users = list(data['users'].values())
    # Apply filter
    if filter_mem:
        filtered = []
        for u in all_users:
            mem = u.get('membership', 'free')
            exp = u.get('expires_at')
            if mem == 'pro' and exp and now > exp:
                mem = 'free'
            if mem == filter_mem:
                filtered.append(u)
        all_users = filtered
    if search:
        all_users = [u for u in all_users if search.lower() in u.get('username', '').lower()]
    # Sort by last_seen desc
    all_users.sort(key=lambda u: u.get('last_seen', ''), reverse=True)
    total = len(all_users)
    offset = (page - 1) * per_page
    page_users = all_users[offset:offset + per_page]
    result_users = []
    for u in page_users:
        mem = u.get('membership', 'free')
        exp = u.get('expires_at')
        if mem == 'pro' and exp and now > exp:
            mem = 'free'
        result_users.append({
            'username': u['username'],
            'user_id': u.get('user_id'),
            'membership': mem,
            'expires_at': exp,
            'first_seen': u.get('first_seen'),
            'last_seen': u.get('last_seen'),
            'payments_count': len(u.get('payments', [])),
            'source': u.get('source', 'auto')
        })
    return {'total': total, 'page': page, 'per_page': per_page, 'users': result_users}

def get_stats():
    data = load_data()
    now = time.time()
    week_ago = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now - 7*86400))
    total = len(data['users'])
    pro = 0
    pro_active = 0
    active_7d = 0
    for u in data['users'].values():
        mem = u.get('membership', 'free')
        exp = u.get('expires_at')
        if mem == 'pro':
            pro += 1
            if exp and now <= exp:
                pro_active += 1
        if u.get('last_seen', '') >= week_ago:
            active_7d += 1
    return {
        'total_users': total,
        'pro_users': pro,
        'pro_active': pro_active,
        'free_users': total - pro,
        'total_payments': len(data.get('payments', [])),
        'active_7d': active_7d
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
        resp = http_requests.request(
            method=request.method,
            url=target_url,
            headers=fwd_headers,
            data=body,
            timeout=15,
        )
        
        # Auto-capture users from register/login/user responses
        try:
            if resp.status_code == 200:
                rjson = resp.json()
                if subpath == 'register' and rjson.get('ok') and rjson.get('user'):
                    u = rjson['user']
                    upsert_user(u['username'], u.get('id'), u.get('membership', 'free'), u.get('expires_at'), 'register')
                elif subpath == 'login' and rjson.get('ok') and rjson.get('user'):
                    u = rjson['user']
                    upsert_user(u['username'], u.get('id'), u.get('membership', 'free'), u.get('expires_at'), 'login')
                elif subpath == 'user' and rjson.get('user') and rjson['user'].get('username'):
                    uname = rjson['user']['username']
                    # Enrich with admin membership data
                    admin_mem, admin_exp, admin_pay = get_membership(uname)
                    if admin_mem != 'free':
                        rjson['user']['membership'] = admin_mem
                        rjson['user']['expires_at'] = admin_exp
                        rjson['admin_payments'] = admin_pay
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
    search = request.args.get('search', None)
    result = list_users(page, per_page, filter_mem, search)
    stats = get_stats()
    return cors_resp({'ok': True, **result, **stats})

@app.route('/admin/user/<username>', methods=['GET', 'OPTIONS'])
def admin_get_user(username):
    if not check_admin():
        return cors_resp({'error': 'Unauthorized'}, 401)
    data = load_data()
    u = data['users'].get(username)
    if not u:
        return cors_resp({'ok': True, 'user': {'username': username, 'membership': 'free', 'expires_at': None, 'payments': []}})
    mem, exp, pay = get_membership(username)
    return cors_resp({'ok': True, 'user': {
        'username': username, 'user_id': u.get('user_id'),
        'membership': mem, 'expires_at': exp,
        'first_seen': u.get('first_seen'), 'last_seen': u.get('last_seen'),
        'source': u.get('source'), 'payments': pay
    }})

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
    data = load_data()
    return cors_resp({'ok': True, 'payments': data.get('payments', [])[-100:]})

# ============ Admin Panel ============
@app.route('/admin')
def admin_panel():
    return send_from_directory('templates', 'admin.html')

@app.route('/health')
def health():
    return jsonify({'ok': True, 'service': '税算寶 API Proxy + Admin', 'target': CF_WORKER_URL})

if __name__ == '__main__':
    os.makedirs('data', exist_ok=True)
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
