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
# MQTT reference implementation kindly provided by "Sansanai" of the dothesmartthings cafe.
#
# Reads 'edgebridge.cfg' for configuration (server port/ip, SmartThings token, data dir).
# Persists '.registrations', 'redirects.jsonl', 'callbacks.jsonl' under the data directory.
#
VERSION = '1.0.1_AEB'

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
import zoneinfo
from collections import OrderedDict
from urllib.parse import unquote

# ====== AEB MQTT BRIDGE dependencies ======
import paho.mqtt.client as mqtt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from cryptography import x509
from cryptography.hazmat.primitives import hashes
# ==========================================

# ====== mDNS / DNS-SD advertisement (optional) ======
try:
    from zeroconf import ServiceInfo, Zeroconf
    HAVE_ZEROCONF = True
except Exception:
    HAVE_ZEROCONF = False
# ====================================================

# ====== HTTP/2 + TLS 1.3 client for /api/forward ======
# Mirrors AndroidEdgeBridge (OkHttp: HTTP/2 via ALPN, TLS 1.2/1.3 negotiated). Tesla's
# owner-api 403s authenticated requests unless the connection is HTTP/2 + TLS 1.3
# (see TeslaMate fixes #5390 / #5406, June 2026).
import ssl
try:
    import httpx
    HAVE_HTTPX = True
except Exception:
    HAVE_HTTPX = False
# ======================================================

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
DASHBOARD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dashboard')
MAXPORT = 65535
TOKEN_LENGTH = 36
DEFAULT_SERVERPORT = 8088
DEFAULT_ST_TOKEN = ''
SERVER_PORT = DEFAULT_SERVERPORT
SERVER_IP = ''
SMARTTHINGS_TOKEN = DEFAULT_ST_TOKEN
FWTIMEOUT = 5

DATA_DIR = os.environ.get('EB_DATA_DIR', os.getcwd())

TIMEZONE = 'UTC'

CALLBACK_NAME_REGEX = re.compile(r'^[A-Za-z0-9_\-]+$')
CALLBACK_MAX_VALUE_BYTES = 64 * 1024
MQTT_RING_MAX = 200

# mDNS advertisement (matches AEB / EdgeBridgeBaseDriver: service type "_edgebridge._tcp")
MDNS_ENABLED = True
MDNS_NAME = 'EdgeBridge-aeb'
MDNS_TYPE = '_edgebridge._tcp.local.'
_zeroconf = None
_mdns_info = None
SERVER_ADVERTISED_IP = ''

# Server start time (for /api/ping)
SERVER_STARTED_AT = int(time.time() * 1000)
SERVER_START_STR = time.strftime('%m/%d %H:%M')

# Build identity (injected by CI at image build time) -- lets you tell builds apart
# even when VERSION is unchanged. 'dev' when run from source.
BUILD_SHA = os.environ.get('EB_BUILD_SHA', 'dev')[:7]
BUILD_DATE = os.environ.get('EB_BUILD_DATE', '')
# Displayed version changes every build (VERSION + git short SHA) so a fresh image is obvious.
DISPLAY_VERSION = VERSION if BUILD_SHA == 'dev' else f'{VERSION}+{BUILD_SHA}'


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
        self.buffer = []   # in-memory ring of recent log lines (for /api/logs dashboard)

    def _ts(self):
        try:
            tz = zoneinfo.ZoneInfo(TIMEZONE)
            return datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")
        except Exception:
            return time.strftime("%Y-%m-%d %H:%M:%S %Z")

    def __savetofile(self, msg):
        with open(self.filename, 'a') as f:
            f.write(f'{self._ts()}  {msg}\n')

    def __outputmsg(self, colormsg, plainmsg, level):
        if self.toconsole:
            print(colormsg)
        if self.savetofile:
            self.__savetofile(plainmsg)
        self.buffer.append({
            'ts': int(time.time() * 1000),
            'level': level,
            'msg': plainmsg,
        })
        if len(self.buffer) > 1000:
            self.buffer.pop(0)

    def info(self, msg):
        self.__outputmsg(f'\033[33m{self._ts()}  \033[96m{msg}\033[0m', msg, 'info')

    def warn(self, msg):
        self.__outputmsg(f'\033[33m{self._ts()}  \033[93m{msg}\033[0m', msg, 'warn')

    def error(self, msg):
        self.__outputmsg(f'\033[33m{self._ts()}  \033[91m{msg}\033[0m', msg, 'error')

    def hilite(self, msg):
        self.__outputmsg(f'\033[33m{self._ts()}  \033[97m{msg}\033[0m', msg, 'hilite')

    def debug(self, msg):
        if len(sys.argv) > 1 and sys.argv[1] == '-d':
            self.__outputmsg(f'\033[33m{self._ts()}  \033[37m{msg}\033[0m', msg, 'debug')


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


def serve_file(server, path, content_type):
    try:
        with open(path, 'rb') as f:
            send_raw(server, 200, f.read(), content_type)
    except FileNotFoundError:
        send_text(server, 404, 'Not found')
    except Exception as e:
        log.error(f'Dashboard file error: {e}')
        send_text(server, 500, 'Dashboard file error')


def serve_dashboard(server, path_only):
    if path_only in ('/dashboard', '/dashboard/'):
        serve_file(server, os.path.join(DASHBOARD_DIR, 'index.html'), 'text/html; charset="utf-8"')
        return True

    prefix = '/dashboard/assets/'
    if not path_only.startswith(prefix):
        return False

    rel_path = path_only[len(prefix):]
    abs_path = os.path.normpath(os.path.join(DASHBOARD_DIR, 'assets', rel_path))
    if not abs_path.startswith(os.path.normpath(os.path.join(DASHBOARD_DIR, 'assets'))):
        send_text(server, 400, 'Invalid asset path')
        return True
    if not os.path.isfile(abs_path):
        send_text(server, 404, 'Not found')
        return True

    content_types = {
        '.css': 'text/css; charset="utf-8"',
        '.js': 'application/javascript; charset="utf-8"',
        '.json': 'application/json; charset="utf-8"',
        '.svg': 'image/svg+xml',
        '.png': 'image/png',
    }
    ext = os.path.splitext(rel_path)[1].lower()
    serve_file(server, abs_path, content_types.get(ext, 'application/octet-stream'))
    return True


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
#  mDNS / DNS-SD advertisement  (so Edge drivers auto-discover the bridge)
#  Service type "_edgebridge._tcp", instance "EdgeBridge*", TXT install_id+version
#  -- matches AEB and the WooBooung/EdgeBridgeBaseDriver discovery code.
#  NOTE: mDNS multicast only works on host/macvlan networking, not Docker bridge.
# =============================================================================

def get_install_id():
    p = data_path('install_id')
    try:
        if os.path.exists(p):
            with open(p) as f:
                v = f.read().strip()
                if v:
                    return v
    except Exception:
        pass
    v = str(uuid.uuid4())
    try:
        with open(p, 'w') as f:
            f.write(v)
    except Exception:
        pass
    return v


def start_mdns(ip, port):
    global _zeroconf, _mdns_info
    if not MDNS_ENABLED:
        return
    if not HAVE_ZEROCONF:
        log.warn('mDNS requested but the "zeroconf" package is not installed -- skipping')
        return
    try:
        info = ServiceInfo(
            MDNS_TYPE,
            f'{MDNS_NAME}.{MDNS_TYPE}',
            addresses=[socket.inet_aton(ip)],
            port=port,
            properties={'install_id': get_install_id(), 'version': VERSION},
            server=f'{MDNS_NAME.replace(" ", "-")}.local.',
        )
        zc = Zeroconf()
        zc.register_service(info)
        _zeroconf, _mdns_info = zc, info
        log.hilite(f' > mDNS advertised as "{MDNS_NAME}" ({MDNS_TYPE}) at {ip}:{port}')
    except Exception as e:
        log.warn(f'mDNS registration failed (expected in Docker bridge mode; use host networking): {e}')


def stop_mdns():
    global _zeroconf, _mdns_info
    try:
        if _zeroconf and _mdns_info:
            _zeroconf.unregister_service(_mdns_info)
            _zeroconf.close()
    except Exception:
        pass
    _zeroconf = None
    _mdns_info = None


# =============================================================================
#  /api/ping  (AEB-compatible health JSON; battery=100 since a server is powered)
# =============================================================================

# SmartThings PAT validity (cached) -- reported as stOauthConnected in /api/ping.
_st_pat_valid = False
_st_pat_checked_at = 0.0
ST_PAT_TTL = 300   # re-validate at most every 5 minutes


def st_pat_valid():
    """True if a PAT is configured in edgebridge.cfg AND SmartThings accepts it
    (any response other than 401). Cached for ST_PAT_TTL seconds."""
    global _st_pat_valid, _st_pat_checked_at
    if not SMARTTHINGS_TOKEN:
        return False
    now = time.time()
    if (now - _st_pat_checked_at) < ST_PAT_TTL:
        return _st_pat_valid
    _st_pat_checked_at = now
    try:
        r = requests.get('https://api.smartthings.com/v1/locations',
                         headers={'Authorization': SMARTTHINGS_TOKEN}, timeout=4)
        _st_pat_valid = (r.status_code != 401)   # 401 = bad/expired token; 200/403 = token accepted
    except Exception as e:
        log.warn(f'PAT validity check failed: {e}')
        _st_pat_valid = False
    return _st_pat_valid


def build_ping():
    sessions = []
    connected = 0
    for s in aeb_sessions.values():
        state = s.get('state', 'CREATED')
        if state == 'CONNECTED':
            connected += 1
        sessions.append({'id': s['id'], 'state': state, 'lastError': s.get('lastError')})
    pat_ok = st_pat_valid()
    return {
        'battery': 100,                 # always powered (not an Android device)
        'bridgeDevice': 'server',
        'bridgeVersion': DISPLAY_VERSION,   # VERSION + git short SHA -> changes every build
        'build': BUILD_SHA,
        'buildDate': BUILD_DATE,
        'serverStartTime': SERVER_START_STR,
        'supportedAiOptions': [],       # LLM not ported
        'stOauthConnected': pat_ok,     # true when the configured PAT is valid
        'stTokenConfigured': bool(SMARTTHINGS_TOKEN),
        'stTokenValid': pat_ok,
        'accessTokenExpiresAt': None,
        'accessTokenMinutesLeft': None,
        'mqtt': {'total': len(aeb_sessions), 'connected': connected, 'sessions': sessions},
        'blocked': {'hosts': 0, 'attempts': 0},
    }


def build_dashboard_summary():
    ping = build_ping()
    registration_items = []
    for item in registrations:
        registration_items.append({
            'devaddr': ':'.join(str(x) for x in item.get('devaddr', []) if x is not None),
            'edgeid': item.get('edgeid'),
            'hubaddr': ':'.join(str(x) for x in item.get('hubaddr', []) if x is not None),
        })

    mqtt_sessions = []
    for session in aeb_sessions.values():
        mqtt_sessions.append({
            'id': session.get('id'),
            'state': session.get('state', 'CREATED'),
            'subscribedTopics': session.get('subscribedTopics', []),
            'forwardTarget': session.get('forwardTarget'),
            'pendingForwardCount': session.get('pendingForwardCount', 0),
            'lastConnectedTs': session.get('lastConnectedTs'),
            'lastForwardOkTs': session.get('lastForwardOkTs'),
            'lastError': session.get('lastError'),
            'effectiveClientId': session.get('effectiveClientId'),
        })

    return {
        'bridge': ping,
        'registrations': registration_items,
        'redirects': list(redirects.values()),
        'callbacks': list(callbacks.values()),
        'mqttSessions': mqtt_sessions,
        'server': {
            'version': VERSION,
            'dataDir': DATA_DIR,
            'serverPort': SERVER_PORT,
            'serverIp': SERVER_IP,
            'mdnsEnabled': MDNS_ENABLED,
            'mdnsName': MDNS_NAME,
        },
        'generatedAt': now_ms(),
    }


def current_settings_snapshot():
    token = SMARTTHINGS_TOKEN[7:] if SMARTTHINGS_TOKEN.startswith('Bearer ') else SMARTTHINGS_TOKEN
    return {
        'forwardingTimeout': FWTIMEOUT,
        'mdnsEnabled': MDNS_ENABLED,
        'mdnsName': MDNS_NAME,
        'stTokenConfigured': bool(token),
        # NOTE: the raw PAT is intentionally NOT returned (was a plaintext token leak via
        # an unauthenticated endpoint). The dashboard shows only configured/valid state.
        'serverIp': SERVER_IP,
        'serverPort': SERVER_PORT,
        'timezone': TIMEZONE,
        'dataDir': DATA_DIR,
        'source': {
            'configFile': os.path.join(os.getcwd(), CONFIGFILENAME),
            'envOverrides': {
                'EB_ST_TOKEN': bool(os.environ.get('EB_ST_TOKEN', '').strip()),
                'EB_FW_TIMEOUT': bool(os.environ.get('EB_FW_TIMEOUT', '').strip()),
                'EB_MDNS_ENABLED': os.environ.get('EB_MDNS_ENABLED', '').strip().lower() in ('no', 'false', '0'),
                'EB_MDNS_NAME': bool(os.environ.get('EB_MDNS_NAME', '').strip()),
                'EB_TZ': bool(os.environ.get('EB_TZ', '').strip()),
            },
        },
    }


def read_existing_config_values():
    values = {}
    parser = configparser.ConfigParser()
    path = os.path.join(os.getcwd(), CONFIGFILENAME)
    if not parser.read(path):
        return values
    try:
        values['Server_IP'] = parser.get('config', 'Server_IP', fallback='')
        values['Server_Port'] = parser.get('config', 'Server_Port', fallback=str(DEFAULT_SERVERPORT))
        values['SmartThings_Bearer_Token'] = parser.get('config', 'SmartThings_Bearer_Token', fallback='')
        values['forwarding_timeout'] = parser.get('config', 'forwarding_timeout', fallback=str(FWTIMEOUT))
        values['console_output'] = parser.get('config', 'console_output', fallback='yes')
        values['logfile_output'] = parser.get('config', 'logfile_output', fallback='no')
        values['logfile'] = parser.get('config', 'logfile', fallback=LOGFILE)
        values['Data_Dir'] = parser.get('config', 'Data_Dir', fallback='')
        values['mDNS_enabled'] = parser.get('config', 'mDNS_enabled', fallback='yes')
        values['mDNS_name'] = parser.get('config', 'mDNS_name', fallback=MDNS_NAME)
        values['Timezone'] = parser.get('config', 'Timezone', fallback='UTC')
    except Exception:
        pass
    return values


def persist_config_file():
    path = os.path.join(os.getcwd(), CONFIGFILENAME)
    token = SMARTTHINGS_TOKEN[7:] if SMARTTHINGS_TOKEN.startswith('Bearer ') else SMARTTHINGS_TOKEN
    existing = read_existing_config_values()
    desired = OrderedDict([
        ('Server_IP', SERVER_IP or existing.get('Server_IP', '')),
        ('Server_Port', str(SERVER_PORT or existing.get('Server_Port', DEFAULT_SERVERPORT))),
        ('SmartThings_Bearer_Token', token),
        ('forwarding_timeout', str(FWTIMEOUT)),
        ('console_output', existing.get('console_output', 'yes')),
        ('logfile_output', existing.get('logfile_output', 'no')),
        ('logfile', existing.get('logfile', LOGFILE)),
        ('Data_Dir', existing.get('Data_Dir', '')),
        ('mDNS_enabled', 'yes' if MDNS_ENABLED else 'no'),
        ('mDNS_name', MDNS_NAME),
        ('Timezone', TIMEZONE),
    ])
    key_lookup = {key.lower(): key for key in desired}
    lines = []
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                lines = f.read().splitlines()
        except Exception as e:
            raise RuntimeError(f'Unable to read config file: {e}')
    if not lines:
        lines = ['[config]']

    out = []
    in_config = False
    seen = set()
    for line in lines:
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith('[') and lowered.endswith(']'):
            if in_config and seen != set(desired):
                for key, value in desired.items():
                    if key not in seen:
                        out.append(f'{key} = {value}')
                        seen.add(key)
            in_config = lowered == '[config]'
            out.append(line)
            continue

        if in_config and '=' in line and not stripped.startswith('#') and not stripped.startswith(';'):
            key, _, _ = line.partition('=')
            lookup = key.strip().lower()
            if lookup in key_lookup:
                canonical = key_lookup[lookup]
                out.append(f'{canonical} = {desired[canonical]}')
                seen.add(canonical)
                continue
        out.append(line)

    if '[config]' not in [l.strip().lower() for l in lines]:
        out = ['[config]'] + [f'{key} = {value}' for key, value in desired.items()]
    else:
        for key, value in desired.items():
            if key not in seen:
                out.append(f'{key} = {value}')

    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(out) + '\n')
    except Exception as e:
        raise RuntimeError(f'Unable to write config file: {e}')


def apply_settings_updates(updates):
    global SMARTTHINGS_TOKEN, FWTIMEOUT, MDNS_ENABLED, MDNS_NAME

    settings_changed = {}
    mdns_prev = MDNS_ENABLED
    mdns_name_prev = MDNS_NAME

    if 'forwardingTimeout' in updates:
        try:
            fw = int(updates['forwardingTimeout'])
            if fw < 1:
                raise ValueError('forwardingTimeout must be >= 1')
            FWTIMEOUT = fw
            settings_changed['forwardingTimeout'] = FWTIMEOUT
        except Exception:
            raise ValueError('forwardingTimeout must be a positive integer')

    if 'mdnsEnabled' in updates:
        mdns_enabled = bool(updates['mdnsEnabled'])
        MDNS_ENABLED = mdns_enabled
        settings_changed['mdnsEnabled'] = MDNS_ENABLED

    if 'mdnsName' in updates:
        name = str(updates['mdnsName']).strip()
        if not name:
            raise ValueError('mDNS name cannot be empty')
        MDNS_NAME = name
        settings_changed['mdnsName'] = MDNS_NAME

    # Only update the token when a non-empty value is supplied. The dashboard no longer
    # pre-fills the token field (security), so it posts an empty value on every save -- that
    # must NOT wipe an existing token. Strip accidental surrounding quotes too.
    token_in = str(updates.get('stToken', '')).strip().strip('"').strip("'").strip()
    if token_in:
        if len(token_in) != TOKEN_LENGTH:
            raise ValueError('SmartThings PAT must be 36 characters')
        SMARTTHINGS_TOKEN = f'Bearer {token_in}'
        settings_changed['stTokenConfigured'] = True

    persist_config_file()
    if MDNS_ENABLED != mdns_prev or MDNS_NAME != mdns_name_prev:
        stop_mdns()
        if MDNS_ENABLED and SERVER_ADVERTISED_IP:
            start_mdns(SERVER_ADVERTISED_IP, SERVER_PORT)
    return settings_changed


# =============================================================================
#  AEB MQTT BRIDGE  (spec: mqtt-bridge-spec-v0.3.md)
#  Reference implementation provided by Sansanai (dothesmartthings cafe).
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

BROWSER_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')


def build_headers(server, path):
    headers = {}
    # 'accept-encoding' is dropped so `requests` performs transparent gzip decompression
    # and we forward already-decompressed bytes (Content-Encoding must NOT be re-advertised).
    ignored = ['host', 'te', 'connection', 'accept-encoding', 'content-length']
    present = set()
    for key, value in server.headers.items():
        if key.lower() not in ignored:
            headers[key] = value
            present.add(key.lower())

    if 'api.smartthings.com' in path:
        if 'authorization' not in present and len(SMARTTHINGS_TOKEN) > 0:
            headers['Authorization'] = SMARTTHINGS_TOKEN

    headers['Host'] = path.split('//')[1].split('/')[0]

    # Browser-like fallbacks (added only if the caller didn't provide them) so that
    # WAF/CDN-protected APIs (Tesla, etc.) don't reject the request with 403.
    if 'user-agent' not in present:
        headers['User-Agent'] = BROWSER_UA
    if 'accept' not in present:
        headers['Accept'] = 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
    if 'accept-language' not in present:
        headers['Accept-Language'] = 'ko-KR,ko;q=0.9,en;q=0.8'

    if server.data_bytes:
        headers['Content-Length'] = str(len(server.data_bytes))
    return headers


_fwd_clients = {}


def _forward_client(url):
    """HTTP/2 forward client. Force TLS 1.3 for Tesla (owner-api 403s authenticated
    requests otherwise -- TeslaMate #5390/#5406); other hosts negotiate TLS normally so
    TLS-1.2-only sites (e.g. some gov APIs) keep working."""
    force_tls13 = ('teslamotors.com' in url) or ('.tesla.com' in url)
    key = 'tls13' if force_tls13 else 'default'
    client = _fwd_clients.get(key)
    if client is None:
        ctx = ssl.create_default_context()
        if force_tls13:
            ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        client = httpx.Client(http2=True, verify=ctx, timeout=FWTIMEOUT,
                              follow_redirects=True, trust_env=False)
        _fwd_clients[key] = client
    return client


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
        if HAVE_HTTPX:
            # HTTP/2 + (Tesla:) TLS 1.3, like AndroidEdgeBridge's OkHttp. httpx sets Host/
            # Content-Length itself; pass our clean header set (Chrome UA, no sec-ch-ua).
            send_headers = {k: v for k, v in headers.items() if k.lower() not in ('host', 'content-length')}
            r = _forward_client(url).request(lc_method.upper(), url, headers=send_headers, content=server.data_bytes)
        else:
            r = getattr(requests, lc_method)(url, data=server.data_bytes, headers=headers, timeout=FWTIMEOUT)
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
        send_json(server, 200, build_ping())
    elif endpoint == 'dashboard':
        send_json(server, 200, build_dashboard_summary())
    elif endpoint == 'logs':
        send_json(server, 200, {'logs': log.buffer})
    elif endpoint == 'settings':
        if method == 'GET':
            send_json(server, 200, current_settings_snapshot())
            return
        if method in ('POST', 'PUT'):
            req = aeb_get_json_body(server)
            try:
                changed = apply_settings_updates(req)
                send_json(server, 200, {
                    'ok': True,
                    'settings': current_settings_snapshot(),
                    'changed': changed,
                })
            except ValueError as e:
                send_json(server, 400, {'error': {'code': 'BAD_SETTINGS', 'message': str(e)}})
            except Exception as e:
                log.error(f'Settings update failed: {e}')
                send_json(server, 500, {'error': {'code': 'SETTINGS_UPDATE_FAILED', 'message': str(e)}})
            return
        send_json(server, 405, {'error': {'code': 'METHOD_NOT_ALLOWED', 'message': 'Unsupported method'}})
        return
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

    # 0) Built-in web dashboard
    if server.command == 'GET' and serve_dashboard(server, path_only):
        return

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
            send_json(self, 200, build_ping())
            return
        proc_msg(self)

    def do_PUT(self):
        proc_msg(self)

    def do_GET(self):
        if '/api/ping' in self.path:
            log.debug('Pingreq')
            send_json(self, 200, build_ping())
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
    global MDNS_ENABLED, MDNS_NAME
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
            # strip whitespace and accidental surrounding quotes (configparser keeps quotes as-is)
            config_token = parser.get('config', 'SmartThings_Bearer_Token').strip().strip('"').strip("'").strip()
            if len(config_token) == TOKEN_LENGTH:
                SMARTTHINGS_TOKEN = 'Bearer ' + config_token
            elif config_token:
                print('\033[31mInvalid SmartThings Token from config file (expected 36 chars); assumed None\033[0m')
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
            if parser.get('config', 'mDNS_enabled').lower() in ('no', 'false', '0'):
                MDNS_ENABLED = False
        except Exception:
            pass
        try:
            name = parser.get('config', 'mDNS_name')
            if name:
                MDNS_NAME = name
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

    # Environment-variable overrides (handy on Docker / Synology Container Manager,
    # where adding an env var is much easier than mounting a config file).
    env_token = os.environ.get('EB_ST_TOKEN', '').strip().strip('"').strip("'").strip()
    if env_token:
        SMARTTHINGS_TOKEN = 'Bearer ' + env_token
    env_port = os.environ.get('EB_SERVER_PORT', '').strip()
    if env_port:
        try:
            p = int(env_port)
            if 0 < p <= MAXPORT:
                SERVER_PORT = p
        except ValueError:
            pass
    env_ip = os.environ.get('EB_SERVER_IP', '').strip()
    if env_ip:
        SERVER_IP = env_ip
    env_fw = os.environ.get('EB_FW_TIMEOUT', '').strip()
    if env_fw:
        try:
            FWTIMEOUT = int(env_fw)
        except ValueError:
            pass
    try:
        config_tz = parser.get('config', 'Timezone').strip()
        if config_tz:
            TIMEZONE = config_tz
    except Exception:
        pass
    env_tz = os.environ.get('EB_TZ', '').strip()
    if env_tz:
        TIMEZONE = env_tz
    if os.environ.get('EB_MDNS_ENABLED', '').strip().lower() in ('no', 'false', '0'):
        MDNS_ENABLED = False
    env_mdns_name = os.environ.get('EB_MDNS_NAME', '').strip()
    if env_mdns_name:
        MDNS_NAME = env_mdns_name

    os.makedirs(DATA_DIR, exist_ok=True)
    log = logger(conoutp, logoutp, logfile, False)


if __name__ == '__main__':
    if platform.system() == 'Windows':
        os.system('color')

    SERVER_STARTED_AT = int(time.time() * 1000)
    SERVER_START_STR = time.strftime('%m/%d %H:%M')

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
        SERVER_ADVERTISED_IP = myip

        log.hilite(f'Forwarding Bridge Server v{DISPLAY_VERSION} ({BUILD_DATE}) [edgebridge-aeb]')
        log.hilite(f' > Serving HTTP on {myip}:{SERVER_PORT}')
        log.hilite(f' > Data directory: {DATA_DIR}')
        log.hilite(f' > Loaded {len(redirects)} redirect(s), {len(callbacks)} callback(s)')

        start_mdns(myip, SERVER_PORT)

        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            log.warn('INFO: Application interrupted by user...\n')
        finally:
            stop_mdns()
