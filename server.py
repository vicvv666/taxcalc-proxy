"""
TaxCalc API Proxy - Reverse proxy to CF Worker
Deployed on Render.com to bypass workers.dev DNS blocking in China
"""
import os
import json
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

CF_WORKER_URL = 'https://taxcalc-api.vichoo2020.workers.dev'

@app.route('/api/<path:subpath>', methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'])
@app.route('/api/', methods=['GET', 'OPTIONS'])
def proxy_api(subpath=''):
    # CORS preflight
    if request.method == 'OPTIONS':
        resp = jsonify({'ok': True})
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        resp.headers['Access-Control-Max-Age'] = '86400'
        return resp

    # Build target URL
    target_url = f"{CF_WORKER_URL}/api/{subpath}"
    if request.query_string:
        target_url += f"?{request.query_string.decode()}"

    # Forward headers
    fwd_headers = {
        'Content-Type': request.headers.get('Content-Type', 'application/json'),
        'Accept': 'application/json',
    }
    if request.headers.get('Authorization'):
        fwd_headers['Authorization'] = request.headers.get('Authorization')

    # Forward body
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
        # Build response with CORS headers
        response = app.response_class(
            response=resp.content,
            status=resp.status_code,
            content_type=resp.headers.get('Content-Type', 'application/json'),
        )
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
        return response
    except Exception as e:
        resp = jsonify({'error': f'Proxy error: {str(e)}'})
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp, 502

@app.route('/health')
def health():
    return jsonify({'ok': True, 'proxy': 'render', 'target': CF_WORKER_URL})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
