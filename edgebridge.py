#
# Copyright 2021, 2022, 2023 Todd Austin
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file
# except in compliance with the License. You may obtain a copy of the License at:
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distributed under the
# License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND,
# either express or implied. See the License for the specific language governing permissions
# and limitations under the License.
#
# DESCRIPTION
#
# Forwarding Bridge Server to support Samsung SmartThings Edge drivers running on a SmartThings hub
#
# edgebridge-aeb : AndroidEdgeBridge (AEB) feature port of toddaustin07/edgebridge.
#   Original features:
#     1. forward HTTP requests from a SmartThings Edge driver to the cloud & return the response
#     2. forward HTTP requests from an IOT device to Edge drivers (via hub)
#   AEB additions (LLM / Bluetooth excluded):
#     3. MQTT bridge      : mTLS subscribe to an external broker & forward messages to the hub
#                           (/mqtt/*  -- see mqtt-bridge-spec-v0.3.md)
#     4. redirect mapping : persistent path -> external URL mapping with auto-proxy (/api/redirect)
#     5. callback store   : store/retrieve arbitrary values by name key (/api/callback)
#     6. forward fixes    : PUT/DELETE/PATCH support + Korean/multi-byte truncation fix
#                           (Content-Length is now the UTF-8 *byte* length, body is raw bytes)
#
# MQTT reference implementation kindly provided by "Sansai-nim" of the dothesmartthings cafe.
#
# Reads 'edgebridge.cfg' for configuration (server port/ip, SmartThings token, data dir).
# Persists '.registrations', 'redirects.jsonl', 'callbacks.jsonl' under the data directory.
#
VERSION = '1.2406221200_AEB'

import http.server
import datetime
import time
import socket
import requests
import os
import sys
import platform
import configparser
import json
import ipaddress
import uuid
import base64
import re
from urllib.parse import unquote

# ====== AEB MQTT BRIDGE dependencies ======
import paho.mqtt.client as mqtt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from cryptography import x509
from cryptography.hazmat.primitives import hashes
# ==========================================

registrations = []
hubsenderrors = {}
regdeletelist = []

aeb_sessions = {}    # MQTT session storage      : sessionId -> session dict
redirects = {}       # redirect registry         : normalized path -> {path, targetBase, createdAt}
callbacks = {}       # callback store            : name -> {name, value, createdAt}

HTTP_OK = 200
CONFIGFILENAME = 'edgebridge.cfg'
REGSFILENAME = '.registrations'
REDIRECTSFILENAME = 'redirects.jsonl'
CALLBACKSFILENAME = 'callbacks.jsonl'
LOGFILE = 'edgebridge.log'
MAXPORT = 65535
TOKEN_LENGTH = 36
DEFAULT_SERVERPORT = 8088
DEFAULT_ST_TOKEN = ''
SERVER_PORT = DEFAULT_SERVERPORT
SERVER_IP = ''
SMARTTHINGS_TOKEN = DEFAULT_ST_TOKEN
FWTIMEOUT = 5

DATA_DIR = os.environ.get('EB_DATA_DIR', os.getcwd())

CALLBACK_NAME_REGEX = re.compile(r'^[A-Za-z0-9_\-]+$')
CALLBACK_MAX_VALUE_BYTES = 64 * 1024
MQTT_RING_MAX = 200


class logger(object):

    def __init__(self, toconsole, tofile, fname, append):
        self.toconsole = toconsole
        self.savetofile = tofile
        self.os = platform.system()
        if self.os == 'Windows':
            os.system('color')
        if tofile:
            self.filename = fname
            if not append:
                try:
                    os.remove(fname)
                except Exception:
                    pass

    def __savetofile(self, msg):
        with open(self.filename, 'a') as f:
            f.write(f'{time.strftime("%c")}  {msg}\n')

    def __outputmsg(self, colormsg, plainmsg):
        if self.toconsole:
            print(colormsg)
        if self.savetofile:
            self.__savetofile(plainmsg)

    def info(self, msg):
        self.__outputmsg(f'\033[33m{time.strftime("%c")}  \033[96m{msg}\033[0m', msg)

    def warn(self, msg):
        self.__outputmsg(f'\033[33m{time.strftime("%c")}  \033[93m{msg}\033[0m', msg)

    def error(self, msg):
        self.__outputmsg(f'\033[33m{time.strftime("%c")}  \033[91m{msg}\033[0m', msg)

    def hilite(self, msg):
        self.__outputmsg(f'\033[33m{time.strftime("%c")}  \033[97m{msg}\033[0m', msg)

    def debug(self, msg):
        if len(sys.argv) > 1 and sys.argv[1] == '-d':
            self.__outputmsg(f'\033[33m{time.strftime("%c")}  \033[37m{msg}\033[0m', msg)


# =============================================================================
#  Generic response helpers  (all Content-Length values are UTF-8 BYTE lengths)
# =============================================================================

def _send(server, code, body_bytes, content_type):
    try:
        server.send_response(code)
        if body_bytes:
            server.send_header('Content-Type', content_type)
            server.send_header('Content-Length', str(len(body_bytes)))
        server.send_header('Date', datetime.datetime.now(datetime.timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT'))
        server.send_header('Server', 'edgeBridge')
        server.end_headers()
        if body_bytes:
            server.wfile.write(body_bytes)
        log.debug('Response sent')
    except Exception as e:
        log.error(f'HTTP Send error: {e}')


def http_response(server, code, responsetosend):
    # Original-compatible text/xml response. Content-Length is the UTF-8 byte length
    # (NOT len(str)) so multi-byte (Korean/CJK) responses are not truncated.
    _send(server, code, responsetosend.encode('utf-8') if responsetosend else b'',
          'text/xml; charset="utf-8"')


def send_json(server, code, data):
    _send(server, code, json.dumps(data).encode('utf-8'), 'application/json; charset="utf-8"')


def send_text(server, code, text):
    _send(server, code, text.encode('utf-8') if text else b'', 'text/plain; charset="utf-8"')


def send_raw(server, code, body_bytes, content_type):
    _send(server, code, body_bytes or b'', content_type or 'application/octet-stream')


# =============================================================================
#  Persistence helpers (JSONL under DATA_DIR)
# =============================================================================

def data_path(filename):
    return os.path.join(DATA_DIR, filename)


def load_jsonl(filename, key_field):
    store = {}
    path = data_path(filename)
    if not os.path.exists(path):
        return store
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    store[rec[key_field]] = rec
    except Exception as e:
        log.error(f'Error loading {filename}: {e}')
    return store


def save_jsonl(filename, store):
    path = data_path(filename)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            for rec in store.values():
                f.write(json.dumps(rec) + '\n')
    except Exception as e:
        log.error(f'Error saving {filename}: {e}')


def now_ms():
    return int(time.time() * 1000)


# =============================================================================
#  AEB MQTT BRIDGE  (spec: mqtt-bridge-spec-v0.3.md)
#  Reference implementation provided by Sansai-nim (dothesmartthings cafe).
# =============================================================================

def _mqtt_cert_dir():
    d = os.path.join(DATA_DIR, 'mqtt_certs')
    os.makedirs(d, exist_ok=True)
    return d


def aeb_get_json_body(server):
    if getattr(server, 'data_bytes', None):
        try:
            return json.loads(server.data_bytes.decode('utf-8'))
        except Exception as e:
            log.error(f'[AEB] JSON parse error: {e}')
    return {}


def aeb_on_message(client, userdata, msg, *args, **kwargs):
    session = userdata
    session['seq'] += 1
    try:
        payload_str = msg.payload.decode('utf-8')
        encoding = 'utf8'
    except UnicodeDecodeError:
        payload_str = base64.b64encode(msg.payload).decode('ascii')
        encoding = 'base64'

    forward_data = {
        'sessionId': session['id'],
        'seq': session['seq'],
        'topic': msg.topic,
        'payload': payload_str,
        'payloadEncoding': encoding,
        'ts': now_ms(),
    }

    # keep a ring buffer (spec section 5) so messages survive a missing forward target
    ring = session['ring']
    ring.append(forward_data)
    if len(ring) > MQTT_RING_MAX:
        del ring[0:len(ring) - MQTT_RING_MAX]

    target = session.get('forwardTarget')
    if not target:
        session['pendingForwardCount'] = len(ring)
        return

    # at-least-once delivery with exponential backoff (500ms -> 4s, up to 4 attempts)
    delay = 0.5
    for attempt in range(4):
        try:
            r = requests.post(target, json=forward_data, timeout=3)
            if 200 <= r.status_code < 300:
                session['lastForwardOkTs'] = now_ms()
                session['pendingForwardCount'] = 0
                return
            log.warn(f"[AEB] Forward HTTP {r.status_code} (attempt {attempt + 1})")
        except Exception as e:
            session['lastError'] = str(e)
            log.warn(f"[AEB] Forward failed (attempt {attempt + 1}): {e}")
        time.sleep(delay)
        delay = min(delay * 2, 4)
    session['lastError'] = 'forward dropped after 4 attempts'
    log.error(f"[AEB] Forward dropped for {session['id']} seq={session['seq']}")


def aeb_on_connect(client, userdata, *args):
    # paho v2 (VERSION2) signature: (client, userdata, connect_flags, reason_code, properties)
    session = userdata
    reason = args[-2] if len(args) >= 2 else 0
    try:
        failed = bool(reason.is_failure)
    except AttributeError:
        failed = (int(reason) != 0)
    if failed:
        session['state'] = 'ERROR'
        session['lastError'] = f'CONNACK failed: {reason}'
        log.error(f"[AEB] {session['id']} CONNACK failed: {reason}")
        return
    session['state'] = 'CONNECTED'
    session['lastConnectedTs'] = now_ms()
    for topic in session.get('subscribedTopics', []):
        client.subscribe(topic, qos=session.get('qos', 1))
    log.info(f"[AEB] {session['id']} CONNECTED; subscribed {session.get('subscribedTopics')}")


def aeb_on_disconnect(client, userdata, *args):
    session = userdata
    if session.get('state') == 'CONNECTED':
        session['state'] = 'DISCONNECTED'
    log.warn(f"[AEB] {session['id']} disconnected")


def handle_aeb_routes(server):
    path = server.path.split('?')[0]
    method = server.command
    parts = path.split('/')   # ['', 'mqtt', 'sessions', '{id}', 'verb']

    try:
        # POST /mqtt/sessions  -- create session + RSA2048 keypair + PKCS#10 CSR
        if method == 'POST' and path == '/mqtt/sessions':
            req = aeb_get_json_body(server)
            session_id = f"sess_{uuid.uuid4().hex[:12]}"
            private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            subject_cn = req.get('subjectCN', 'AEB Bridge Certificate')
            csr = x509.CertificateSigningRequestBuilder().subject_name(
                x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject_cn)])
            ).sign(private_key, hashes.SHA256())
            csr_pem = csr.public_bytes(serialization.Encoding.PEM).decode('utf-8')

            aeb_sessions[session_id] = {
                'id': session_id,
                'state': 'CREATED',
                'private_key': private_key,
                'seq': 0,
                'client': None,
                'subscribedTopics': [],
                'qos': 1,
                'forwardTarget': None,
                'pendingForwardCount': 0,
                'lastConnectedTs': None,
                'lastForwardOkTs': None,
                'lastError': None,
                'ring': [],
            }
            log.info(f'[AEB] Created session {session_id}')
            send_json(server, 201, {'sessionId': session_id, 'csrPem': csr_pem, 'state': 'CREATED'})
            return True

        # POST /mqtt/sessions/{id}/connect
        if method == 'POST' and len(parts) == 5 and parts[4] == 'connect':
            session_id = parts[3]
            session = aeb_sessions.get(session_id)
            if not session:
                send_json(server, 404, {'error': {'code': 'SESSION_NOT_FOUND', 'message': 'Not found'}})
                return True

            req = aeb_get_json_body(server)
            topics = req.get('topics', [])
            if not topics:
                send_json(server, 400, {'error': {'code': 'NO_TOPICS', 'message': 'topics requires >= 1 entry'}})
                return True
            qos = req.get('qos', 1)
            if qos not in (0, 1):
                send_json(server, 400, {'error': {'code': 'BAD_QOS', 'message': 'qos must be 0 or 1'}})
                return True

            cdir = _mqtt_cert_dir()
            cert_path = os.path.join(cdir, f'{session_id}.crt')
            key_path = os.path.join(cdir, f'{session_id}.key')
            with open(cert_path, 'w') as f:
                f.write(req['certPem'])
            with open(key_path, 'wb') as f:
                f.write(session['private_key'].private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.TraditionalOpenSSL,
                    encryption_algorithm=serialization.NoEncryption()))

            client_id = req.get('clientId') or f'aeb-{session_id}'
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
            client.user_data_set(session)
            client.on_message = aeb_on_message
            client.on_connect = aeb_on_connect
            client.on_disconnect = aeb_on_disconnect

            if req.get('caPem'):
                ca_path = os.path.join(cdir, f'{session_id}_ca.crt')
                with open(ca_path, 'w') as f:
                    f.write(req['caPem'])
                client.tls_set(certfile=cert_path, keyfile=key_path, ca_certs=ca_path)
            else:
                client.tls_set(certfile=cert_path, keyfile=key_path)

            session['subscribedTopics'] = topics
            session['qos'] = qos
            session['effectiveClientId'] = client_id
            session['client'] = client
            session['state'] = 'CONNECTING'
            session['lastError'] = None

            try:
                # SNI is derived from the endpoint hostname automatically by paho (server_hostname).
                client.connect(req['endpoint'], req.get('port', 8883), req.get('keepAliveSec', 60))
                client.loop_start()
                log.info(f"[AEB] {session_id} connecting to {req['endpoint']}:{req.get('port', 8883)}")
                send_json(server, 200, {
                    'sessionId': session_id,
                    'state': 'CONNECTING',
                    'subscribedTopics': topics,
                })
            except Exception as e:
                session['state'] = 'ERROR'
                session['lastError'] = str(e)
                log.error(f'[AEB] Connect failed: {e}')
                send_json(server, 500, {'error': {'code': 'CONNECT_FAILED', 'message': str(e)}})
            return True

        # PUT /mqtt/sessions/{id}/forward
        if method == 'PUT' and len(parts) == 5 and parts[4] == 'forward':
            session_id = parts[3]
            session = aeb_sessions.get(session_id)
            if not session:
                send_json(server, 404, {'error': {'code': 'SESSION_NOT_FOUND', 'message': 'Not found'}})
                return True
            req = aeb_get_json_body(server)
            hub_ip = req.get('hubAddress') or server.client_address[0]
            hub_port = req['hubPort']
            fwd_path = req.get('path') or '/aeb/ingest'
            forward_target = f'http://{hub_ip}:{hub_port}{fwd_path}'
            session['forwardTarget'] = forward_target
            log.info(f'[AEB] {session_id} forward target: {forward_target}')
            # flush any buffered messages (FIFO) on registration
            if session['ring']:
                buffered = list(session['ring'])
                session['ring'].clear()
                for item in buffered:
                    try:
                        requests.post(forward_target, json=item, timeout=3)
                        session['lastForwardOkTs'] = now_ms()
                    except Exception as e:
                        session['lastError'] = str(e)
                session['pendingForwardCount'] = 0
            send_json(server, 200, {'sessionId': session_id, 'forwardTarget': forward_target})
            return True

        # GET /mqtt/sessions/{id}/status
        if method == 'GET' and len(parts) == 5 and parts[4] == 'status':
            session_id = parts[3]
            session = aeb_sessions.get(session_id)
            if not session:
                send_json(server, 404, {'error': {'code': 'SESSION_NOT_FOUND', 'message': 'Not found'}})
                return True
            status = {
                'sessionId': session_id,
                'state': session.get('state', 'CREATED'),
                'subscribedTopics': session.get('subscribedTopics', []),
                'forwardTarget': session.get('forwardTarget'),
                'pendingForwardCount': session.get('pendingForwardCount', 0),
                'lastConnectedTs': session.get('lastConnectedTs'),
                'lastForwardOkTs': session.get('lastForwardOkTs'),
                'lastError': session.get('lastError'),
            }
            if session.get('effectiveClientId'):
                status['effectiveClientId'] = session['effectiveClientId']
                status['liveClientIdConnections'] = 1 if session.get('state') == 'CONNECTED' else 0
            send_json(server, 200, status)
            return True

        # GET /mqtt/sessions/{id}/messages?since=
        if method == 'GET' and len(parts) == 5 and parts[4] == 'messages':
            session_id = parts[3]
            session = aeb_sessions.get(session_id)
            if not session:
                send_json(server, 404, {'error': {'code': 'SESSION_NOT_FOUND', 'message': 'Not found'}})
                return True
            since = 0
            q = server.path.split('?', 1)
            if len(q) == 2 and q[1].startswith('since='):
                try:
                    since = int(q[1][6:])
                except ValueError:
                    since = 0
            msgs = [m for m in session['ring'] if m['seq'] > since]
            cursor = str(msgs[-1]['seq']) if msgs else str(since)
            send_json(server, 200, {'messages': msgs, 'cursor': cursor})
            return True

        # DELETE /mqtt/sessions/{id}
        if method == 'DELETE' and len(parts) == 4:
            session_id = parts[3]
            session = aeb_sessions.pop(session_id, None)
            if session and session.get('client'):
                try:
                    session['client'].loop_stop()
                    session['client'].disconnect()
                except Exception:
                    pass
            # erase keys/certs
            for suffix in ('.crt', '.key', '_ca.crt'):
                p = os.path.join(_mqtt_cert_dir(), f'{session_id}{suffix}')
                try:
                    os.remove(p)
                except OSError:
                    pass
            send_json(server, 200, {'sessionId': session_id, 'deleted': True})
            return True

    except Exception as e:
        log.error(f'[AEB] Error: {e}')
        send_json(server, 500, {'error': {'code': 'INTERNAL_ERROR', 'message': str(e)}})
        return True

    return False


# =============================================================================
#  /api/forward  (original feature + multi-byte truncation fix + PUT/DELETE/PATCH)
# =============================================================================

def build_headers(server, path):
    headers = {}
    # 'accept-encoding' is dropped so `requests` performs transparent gzip decompression
    # and we forward already-decompressed bytes (Content-Encoding must NOT be re-advertised).
    ignored = ['user-agent', 'host', 'te', 'connection', 'accept-encoding', 'content-length']
    for key, value in server.headers.items():
        if key.lower() not in ignored:
            headers[key] = value

    if 'api.smartthings.com' in path:
        if 'authorization' not in map(str.lower, server.headers.keys()):
            if len(SMARTTHINGS_TOKEN) > 0:
                headers['Authorization'] = SMARTTHINGS_TOKEN

    headers['Host'] = path.split('//')[1].split('/')[0]
    if 'accept' not in map(str.lower, server.headers.keys()):
        headers['Accept'] = '*/*'
    headers['User-Agent'] = 'SmartThings Edge Hub'

    if server.data_bytes:
        headers['Content-Length'] = str(len(server.data_bytes))
    return headers


def proc_forward(server, method, path, arg):
    if not arg.startswith('url='):
        log.error('Missing URL from forward command')
        http_response(server, 400, '')
        return

    url = path[path.index('url=') + 4:]
    log.info(f'Sending {method} to {url}')
    headers = build_headers(server, path)
    log.debug(f'Headers: {headers}')

    lc_method = method.lower()
    if lc_method not in ('post', 'put', 'get', 'delete', 'patch'):
        log.error(f'Unsupported forward method: {method}')
        http_response(server, 405, '')
        return

    try:
        r = getattr(requests, lc_method)(url, data=server.data_bytes, headers=headers, timeout=FWTIMEOUT)
    except requests.Timeout:
        log.error('Internet request timed out')
        send_raw(server, 502, b'', None)
        return
    except Exception as e:
        log.error(f'Forward error: {e}')
        send_raw(server, 502, f'Bad Gateway: {e}'.encode('utf-8'), 'text/plain; charset="utf-8"')
        return

    # Forward the RAW upstream bytes with a byte-accurate Content-Length so that
    # multi-byte (Korean/CJK) bodies are not truncated, and pass the Content-Type through.
    ctype = r.headers.get('Content-Type', 'application/octet-stream')
    log.debug(f'Returned {r.status_code}, {len(r.content)} bytes')
    send_raw(server, r.status_code, r.content, ctype)
    if r.status_code == HTTP_OK:
        log.info(f'Response returned to Edge driver ({len(r.content)} bytes)')
    else:
        log.warn(f'HTTP {r.status_code} returned to Edge driver')


# =============================================================================
#  /api/redirect  (persistent path -> URL mapping + inbound auto-proxy)
# =============================================================================

def normalize_redirect_path(path):
    trimmed = path.strip()
    with_slash = trimmed if trimmed.startswith('/') else '/' + trimmed
    return (with_slash.rstrip('/') or '/').lower()


def find_redirect_match(request_path):
    lower = request_path.lower()
    best = None
    for reg in redirects.values():
        p = reg['path']
        if lower == p or lower.startswith(p + '/'):
            if best is None or len(p) > len(best['path']):
                best = reg
    return best


def query_param(server, name):
    q = server.path.split('?', 1)
    if len(q) != 2:
        return None
    for pair in q[1].split('&'):
        if pair.startswith(name + '='):
            return unquote(pair[len(name) + 1:])
    return None


def handle_redirect(server, method):
    if method == 'POST':
        path = query_param(server, 'path')
        target = query_param(server, 'target')
        if not path or not target:
            send_text(server, 400, 'Missing required parameters: path, target')
            return
        if not (target.lower().startswith('http://') or target.lower().startswith('https://')):
            send_text(server, 400, 'target must start with http:// or https://')
            return
        norm = normalize_redirect_path(path)
        redirects[norm] = {'path': norm, 'targetBase': target.rstrip('/'), 'createdAt': now_ms()}
        save_jsonl(REDIRECTSFILENAME, redirects)
        log.info(f'Redirect registered: {norm} -> {target}')
        send_text(server, 200, '')
    elif method == 'DELETE':
        path = query_param(server, 'path')
        if not path:
            send_text(server, 400, 'Missing required parameter: path')
            return
        redirects.pop(normalize_redirect_path(path), None)
        save_jsonl(REDIRECTSFILENAME, redirects)
        send_text(server, 200, '')
    elif method == 'GET':
        send_json(server, 200, list(redirects.values()))
    else:
        send_text(server, 405, '')


# =============================================================================
#  /api/callback  (store/retrieve arbitrary values by name key)
# =============================================================================

def handle_callback(server, method, parts):
    # parts == ['', 'api', 'callback', '{name}'?]
    if method == 'POST':
        name = query_param(server, 'name')
        if not name:
            send_text(server, 400, 'Missing required parameter: name')
            return
        if not CALLBACK_NAME_REGEX.match(name):
            send_text(server, 400, 'Invalid name (allowed: [A-Za-z0-9_-])')
            return
        value = server.data_bytes.decode('utf-8') if getattr(server, 'data_bytes', None) else ''
        if len(value.encode('utf-8')) > CALLBACK_MAX_VALUE_BYTES:
            send_text(server, 400, f'value too large (max {CALLBACK_MAX_VALUE_BYTES} bytes)')
            return
        callbacks[name] = {'name': name, 'value': value, 'createdAt': now_ms()}
        save_jsonl(CALLBACKSFILENAME, callbacks)
        log.info(f'Callback stored: {name}')
        send_text(server, 200, '')
    elif method == 'DELETE':
        name = query_param(server, 'name')
        if not name:
            send_text(server, 400, 'Missing required parameter: name')
            return
        callbacks.pop(name, None)
        save_jsonl(CALLBACKSFILENAME, callbacks)
        send_text(server, 200, '')
    elif method == 'GET':
        if len(parts) >= 4 and parts[3]:
            name = parts[3]
            entry = callbacks.get(name)
            if entry is None:
                send_text(server, 404, f'Not found: {name}')
                return
            send_text(server, 200, entry['value'])
        else:
            send_json(server, 200, list(callbacks.values()))
    else:
        send_text(server, 405, '')


# =============================================================================
#  Device -> Hub forwarding (original feature, unchanged)
# =============================================================================

def error_proc(hubaddr):
    key = f'{hubaddr[0]}:{hubaddr[1]}'
    if key in hubsenderrors:
        errcount = hubsenderrors[key] + 1
        if errcount == 3:
            del hubsenderrors[key]
            for item in registrations:
                if item['hubaddr'] == hubaddr:
                    regdeletelist.append(item)
        else:
            hubsenderrors[key] = errcount
    else:
        hubsenderrors[key] = 1


def passto_hub(server, regrecord):
    headers = {}
    hubaddr = regrecord['hubaddr'][0] + ':' + str(regrecord['hubaddr'][1])
    if regrecord['devaddr'][1] is not None:
        devaddr = regrecord['devaddr'][0] + ':' + str(regrecord['devaddr'][1])
    else:
        devaddr = regrecord['devaddr'][0]

    url = 'http://' + hubaddr + '/' + devaddr + '/' + server.command + server.path
    headers['Host'] = hubaddr
    if server.data_bytes and len(server.data_bytes) > 0:
        headers['Content-Length'] = str(len(server.data_bytes))
        if 'Content-Type' in server.headers:
            headers['Content-Type'] = server.headers['Content-Type']

    log.info(f'Sending POST: {url} to {hubaddr}')
    try:
        r = requests.post(url, headers=headers, data=server.data_bytes)
        if r.status_code == 200:
            log.info(f"Message forwarded to Edge ID {regrecord['edgeid']}")
        else:
            log.error(f"ERROR sending message to Edge hub {regrecord['hubaddr']}: {r.status_code}")
    except Exception:
        log.error(f"FAILED sending message to Edge hub {regrecord['hubaddr']}")
        error_proc(regrecord['hubaddr'])


def verify_addr(addrstr):
    port = None
    if not addrstr:
        return False
    if ':' in addrstr:
        addrparts = addrstr.split(':')
        ip = addrparts[0]
        port = int(addrparts[1])
        if (port < 1) or (port > MAXPORT):
            log.error(f'Invalid port number: {port}')
            return False
    else:
        ip = addrstr

    if ip:
        ipparts = ip.split('.')
        if len(ipparts) == 4:
            try:
                if all(0 <= int(p) < 256 for p in ipparts):
                    return (ip, port)
            except Exception:
                log.error(f'Invalid IP address syntax: {ipparts}')
    log.error(f'Invalid IP address: {ip}')
    return False


def verify_ID(id):
    idprofile = [8, 4, 4, 4, 12]
    id = id.lower()
    parts = id.split('-')
    if len(parts) == len(idprofile):
        for i in range(len(parts)):
            if len(parts[i]) == idprofile[i]:
                for x in range(idprofile[i]):
                    if parts[i][x] not in '0123456789abcdef':
                        return False
            else:
                return False
    else:
        return False
    return id


def find_reg(reglist, devaddr, edgeid):
    for index in range(len(reglist)):
        if reglist[index]['devaddr'] == devaddr and reglist[index]['edgeid'] == edgeid:
            return index
    return None


def read_regs(regs_filename):
    file_path = data_path(regs_filename)
    try:
        with open(file_path, 'r') as f1:
            reglist = []
            for line in f1.readlines():
                reglist.append(json.loads(line))
            return reglist
    except Exception:
        log.warn('INFO: No existing registrations')
        return []


def write_regs(regs_filename, reglist):
    file_path = data_path(regs_filename)
    try:
        with open(file_path, 'w') as f1:
            for reg in reglist:
                f1.write(json.dumps(reg) + '\n')
    except Exception:
        log.error('Error saving registrations')


def proc_register(server, method, arglist):
    devaddr = hubaddr = edgeid = None
    for arg in arglist:
        if arg.startswith('devaddr='):
            devaddr = verify_addr(arg[8:])
        elif arg.startswith('hubaddr='):
            hubaddr = verify_addr(arg[8:])
        elif arg.startswith('edgeid='):
            edgeid = verify_ID(arg[7:])
        else:
            log.error('Unrecognized argument in register command')
            http_response(server, 400, '')
            return

    if devaddr and hubaddr and edgeid:
        index = find_reg(registrations, devaddr, edgeid)
        if method in ['post', 'Post', 'POST']:
            log.info(f'Request to register device at {devaddr}')
            if index is None:
                registrations.append({'devaddr': devaddr, 'edgeid': edgeid, 'hubaddr': hubaddr})
                log.info('Registration record ADDED')
            else:
                registrations[index] = {'devaddr': devaddr, 'edgeid': edgeid, 'hubaddr': hubaddr}
                log.info('Existing registration was REPLACED')
            http_response(server, 200, '')
        elif method in ['delete', 'Delete', 'DELETE']:
            log.info(f'Request to remove registration {devaddr}')
            if index is not None:
                del registrations[index]
                log.info(f'Registration {index} DELETED')
                http_response(server, 200, '')
            else:
                log.warn(f'Request to remove address that is not registered: {devaddr}')
                http_response(server, 404, '')
        else:
            log.error(f'Invalid method provided ({method}) for register command')
            http_response(server, 405, '')
    else:
        log.error('Missing argument(s) in register command')
        http_response(server, 400, '')

    log.info(f'Updated registrations: {registrations}')
    write_regs(REGSFILENAME, registrations)


def proc_registered_requests(server):
    global regdeletelist, registrations
    regfound = False
    for record in registrations:
        match = False
        if record['devaddr'][0] == server.client_address[0]:
            match = True
            if record['devaddr'][1] and record['devaddr'][1] != server.client_address[1]:
                match = False
            if match:
                regfound = True
                log.info('>>>>> Forwarding to SmartThings hub')
                passto_hub(server, record)

    if regfound:
        http_response(server, 200, '')
        for item in regdeletelist:
            log.info(f'Scrubbing registration record: {item}')
            registrations.remove(item)
        if len(regdeletelist) > 0:
            write_regs(REGSFILENAME, registrations)
            regdeletelist.clear()
        return True
    return False


# =============================================================================
#  Request dispatch
# =============================================================================

def handle_api(server):
    method = server.command
    path_only = server.path.split('?')[0]
    parts = path_only.split('/')   # ['', 'api', '<endpoint>', ...]
    endpoint = parts[2].lower() if len(parts) > 2 else ''

    if endpoint == 'forward':
        arg = server.path.split('?', 1)
        proc_forward(server, method, server.path, arg[1].split('&')[0] if len(arg) == 2 else '')
    elif endpoint == 'register':
        arg = server.path.split('?', 1)
        proc_register(server, method, arg[1].split('&') if len(arg) == 2 else [])
    elif endpoint == 'redirect':
        handle_redirect(server, method)
    elif endpoint == 'callback':
        handle_callback(server, method, parts)
    elif endpoint == 'ping':
        # original behaviour: simple 200 (AEB battery/bridgeDevice ping is intentionally NOT ported)
        http_response(server, 200, '')
    elif endpoint == 'llm':
        # LLM endpoint intentionally NOT ported
        send_json(server, 404, {'error': {'code': 'NOT_SUPPORTED', 'message': 'LLM endpoint not available in edgebridge-aeb'}})
    else:
        log.warn(f'Invalid endpoint: {endpoint}')
        http_response(server, 404, '')


def proc_msg(server):
    log.info('**********************************************************************************')
    log.info(f'{server.command} request received from: {server.client_address}')
    log.debug(f'Endpoint: {server.path}')

    server.data_bytes = None
    if 'Content-Length' in server.headers:
        server.data_bytes = server.rfile.read(int(server.headers['Content-Length']))

    path_only = server.path.split('?')[0]

    # 1) MQTT bridge traffic
    if path_only.startswith('/mqtt/'):
        if handle_aeb_routes(server):
            return

    # 2) Management/forward API
    if path_only.startswith('/api/'):
        handle_api(server)
        return

    # 3) Inbound from a registered IOT device -> forward to hub
    if proc_registered_requests(server):
        return

    # 4) Inbound auto-proxy via redirect mapping (302)
    match = find_redirect_match(path_only)
    if match:
        suffix = path_only[len(match['path']):]
        query = server.path.split('?', 1)
        location = match['targetBase'].rstrip('/') + suffix
        if len(query) == 2 and query[1]:
            location += '?' + query[1]
        log.info(f'Redirect proxy: {path_only} -> {location}')
        server.send_response(302)
        server.send_header('Location', location)
        server.send_header('Server', 'edgeBridge')
        server.end_headers()
        return

    log.error('Unregistered address or Invalid endpoint')
    http_response(server, 400, '')


class myHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):

    def do_POST(self):
        if '/api/ping' in self.path:
            log.debug('Pingreq')
            http_response(self, 200, '')
            return
        proc_msg(self)

    def do_PUT(self):
        proc_msg(self)

    def do_GET(self):
        if '/api/ping' in self.path:
            log.debug('Pingreq')
            http_response(self, 200, '')
            return
        proc_msg(self)

    def do_DELETE(self):
        proc_msg(self)

    def do_PATCH(self):
        proc_msg(self)

    def log_message(self, format, *args):
        return


class ThreadingHTTPServer(http.server.ThreadingHTTPServer):
    # Threaded so a slow upstream forward / MQTT flush does not block other requests.
    daemon_threads = True
    allow_reuse_address = True


def process_config(config_filename):
    global SERVER_PORT, SERVER_IP, SMARTTHINGS_TOKEN, FWTIMEOUT, DATA_DIR, log
    SERVER_IP = ''
    SERVER_PORT = DEFAULT_SERVERPORT
    SMARTTHINGS_TOKEN = DEFAULT_ST_TOKEN
    conoutp = True
    logoutp = False
    logfile = ''

    parser = configparser.ConfigParser()
    if parser.read(os.path.join(os.getcwd(), config_filename)):
        try:
            config_ip = ipaddress.ip_address(parser.get('config', 'Server_IP'))
            SERVER_IP = str(config_ip)
        except Exception:
            pass

        try:
            config_port = int(parser.get('config', 'Server_Port'))
            if 0 < config_port <= MAXPORT:
                SERVER_PORT = config_port
            else:
                print(f'\033[31mInvalid port from config file; using default: {DEFAULT_SERVERPORT}\033[0m')
        except Exception:
            print(f'\033[31mMissing port from config file; using default: {DEFAULT_SERVERPORT}\033[0m')

        try:
            config_token = parser.get('config', 'SmartThings_Bearer_Token')
            if len(config_token) == TOKEN_LENGTH:
                SMARTTHINGS_TOKEN = 'Bearer ' + config_token
            else:
                print('\033[31mInvalid SmartThings Token from config file; assumed None\033[0m')
        except Exception:
            pass

        try:
            if parser.get('config', 'forwarding_timeout'):
                FWTIMEOUT = int(parser.get('config', 'forwarding_timeout'))
        except Exception:
            pass

        # Data directory: env EB_DATA_DIR wins, else config Data_Dir, else cwd
        if not os.environ.get('EB_DATA_DIR'):
            try:
                d = parser.get('config', 'Data_Dir')
                if d:
                    DATA_DIR = d
            except Exception:
                pass

        try:
            conoutp = parser.get('config', 'console_output').lower() == 'yes'
            if parser.get('config', 'logfile_output').lower() == 'yes':
                logoutp = True
                logfile = parser.get('config', 'logfile')
            else:
                logoutp = False
                logfile = ''
        except Exception:
            print('Using output config defaults')

    os.makedirs(DATA_DIR, exist_ok=True)
    log = logger(conoutp, logoutp, logfile, False)


if __name__ == '__main__':
    if platform.system() == 'Windows':
        os.system('color')

    process_config(CONFIGFILENAME)
    registrations = read_regs(REGSFILENAME)
    redirects = load_jsonl(REDIRECTSFILENAME, 'path')
    callbacks = load_jsonl(CALLBACKSFILENAME, 'name')

    try:
        httpd = ThreadingHTTPServer((SERVER_IP, SERVER_PORT), myHTTPRequestHandler)
    except OSError as error:
        log.error(f'ERROR: cannot initialize Server; {error}')
        log.warn(f'Invalid IP address or Port {SERVER_PORT} may be in use by another application\n')
        httpd = False

    if httpd:
        if SERVER_IP == '':
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            myip = s.getsockname()[0]
            s.close()
        else:
            myip = SERVER_IP

        log.hilite(f'Forwarding Bridge Server v{VERSION} (for SmartThings Edge) [edgebridge-aeb]')
        log.hilite(f' > Serving HTTP on {myip}:{SERVER_PORT}')
        log.hilite(f' > Data directory: {DATA_DIR}')
        log.hilite(f' > Loaded {len(redirects)} redirect(s), {len(callbacks)} callback(s)')

        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            log.warn('INFO: Application interrupted by user...\n')
