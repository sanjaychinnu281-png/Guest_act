# ==================== IMPORTS ====================
import sys
import asyncio
import httpx
import random
import json
import socket
import struct
import time
import os
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any

if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

from google_play_scraper import app as play_scraper
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from protobuf_decoder.protobuf_decoder import Parser

# ⚡ यहाँ तुम्हारी नई pb2 file का नाम है
import CHINNUTEAM_pb2 as MASTERTeam_pb2


# ==================== CONFIG ====================
TOKEN_CACHE_FILE = "token_cache.json"
DEVICES_FILE = "devices.json"
TOKEN_CACHE_TTL = 3600

BATCH_SIZE = 30
BATCH_INTERVAL = 0.5
ONLINE_DURATION = 3.0
LOGIN_CONCURRENCY = 60
MAX_CONNECTIONS = 1000
MAX_KEEPALIVE = 500
HOST_RACE_TIMEOUT = 6.0
TCP_CONNECT_TIMEOUT = 10.0

ACTIVE_OUTPUT_FILE = "active_accounts.json"

REGIONAL_LOGIN_HOSTS = {
    "IND": "https://client.ind.freefiremobile.com/",
    "BD":  "https://client.bd.freefiremobile.com/",
    "SG":  "https://client.sg.freefiremobile.com/",
    "BR":  "https://client.br.freefiremobile.com/",
    "US":  "https://client.us.freefiremobile.com/",
    "EU":  "https://client.eu.freefiremobile.com/",
    "ME":  "https://client.me.freefiremobile.com/",
}

MAJORLOGIN_FALLBACK_HOSTS = [
    "https://client.ind.freefiremobile.com/",
    "https://client.bd.freefiremobile.com/",
    "https://loginbp.ppmainecoonghj.com/",
    "https://loginbp.ggmainecoonghj.com/",
    "https://loginbp.ggblueshark.com/",
    "https://loginbp.commonmainecoonghj.com/",
    "https://indnetwork.ggblueshark.com/",
    "https://indgigateway.ggblueshark.com/",
]

AES_KEY = b'Yg&tc%DEuh6%Zc^8'
AES_IV = b'6oyZDr22E3ychjM%'


# ==================== LOG HELPERS ====================
def _ts() -> str:
    return time.strftime('%H:%M:%S')


def log_ok(t):
    print(f"[{_ts()}] [OK]   {t}")


def log_err(t):
    print(f"[{_ts()}] [ERR]  {t}")


def log_warn(t):
    print(f"[{_ts()}] [WARN] {t}")


def log_info(t):
    print(f"[{_ts()}] [INFO] {t}")


print_success = log_ok
print_error   = log_err
print_warning = log_warn
print_info    = log_info
print_active  = log_ok


# ==================== DEVICE RANDOMIZER ====================
_DEVICE_POOL = [
    ("Samsung", "SM-G998B",   "Adreno (TM) 660", "Android OS 12 / API-31"),
    ("Xiaomi",  "2201122G",   "Adreno (TM) 730", "Android OS 13 / API-33"),
    ("Realme",  "RMX3700",    "Mali-G710",       "Android OS 14 / API-34"),
    ("OnePlus", "CPH2451",    "Adreno (TM) 740", "Android OS 13 / API-33"),
    ("OPPO",    "CPH2611",    "Adreno (TM) 720", "Android OS 14 / API-34"),
    ("Vivo",    "V2203",      "Mali-G710",       "Android OS 12 / API-31"),
    ("Poco",    "M2102J20SG", "Adreno (TM) 660", "Android OS 13 / API-33"),
]

_DEVICES_LOCK = asyncio.Lock()
_DEVICES_CACHE: Optional[dict] = None


def _generate_new_device() -> dict:
    brand, model, gpu, os_ver = random.choice(_DEVICE_POOL)
    return {
        "unique_device_id": f"Google|{uuid.uuid4()}",
        "brand": brand,
        "model": model,
        "gpu_renderer": gpu,
        "system_software": os_ver,
        "screen_width": random.choice([1080, 1440, 720, 1280]),
        "screen_height": random.choice([2400, 3200, 1600, 2400]),
        "screen_dpi": str(random.randint(300, 420)),
        "memory": random.randint(2800, 6500),
        "processor_details": f"ARM64 FP ASIMD AES VMH | {random.randint(2200, 3200)} | {random.randint(6, 12)}",
        "client_ip": f"{random.randint(103, 223)}.{random.randint(10, 250)}.{random.randint(10, 250)}.{random.randint(10, 250)}",
    }


def _load_devices_sync() -> dict:
    if not os.path.exists(DEVICES_FILE):
        return {}
    try:
        with open(DEVICES_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_devices_sync(devices: dict):
    try:
        tmp = DEVICES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(devices, f, indent=4)
        os.replace(tmp, DEVICES_FILE)
    except Exception:
        pass


async def get_device_for_account(account_identifier: str) -> dict:
    global _DEVICES_CACHE
    async with _DEVICES_LOCK:
        if _DEVICES_CACHE is None:
            loop = asyncio.get_running_loop()
            _DEVICES_CACHE = await loop.run_in_executor(None, _load_devices_sync)

        acc_key = str(account_identifier).strip()
        if acc_key in _DEVICES_CACHE:
            return _DEVICES_CACHE[acc_key]

        new_device = _generate_new_device()
        _DEVICES_CACHE[acc_key] = new_device
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _save_devices_sync, dict(_DEVICES_CACHE))
        return new_device


# ==================== DNS ====================
_DNS_CACHE: Dict[str, Tuple[str, float]] = {}
_DNS_CACHE_TTL = 600.0


async def resolve_host(hostname: str) -> str:
    if not hostname:
        return hostname
    parts = hostname.split('.')
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return hostname

    now = time.time()
    cached = _DNS_CACHE.get(hostname)
    if cached and now < cached[1]:
        return cached[0]

    try:
        loop = asyncio.get_running_loop()
        ip_info = await loop.getaddrinfo(hostname, None, family=socket.AF_INET)
        if ip_info:
            ip = ip_info[0][4][0]
            _DNS_CACHE[hostname] = (ip, now + _DNS_CACHE_TTL)
            return ip
    except Exception:
        pass
    return hostname


def optimize_tcp_socket(sock: socket.socket):
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 262144)
    except Exception:
        pass


# ==================== NETWORK ====================
client = httpx.AsyncClient(
    verify=False,
    timeout=httpx.Timeout(15.0, connect=6.0),
    limits=httpx.Limits(
        max_connections=MAX_CONNECTIONS,
        max_keepalive_connections=MAX_KEEPALIVE,
    ),
    follow_redirects=True,
    http2=True,
)

_LOGIN_SEMAPHORE = asyncio.Semaphore(LOGIN_CONCURRENCY)

_BASE_HEADERS = {
    'User-Agent': 'UnityPlayer/2018.4.12f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)',
    'Connection': 'Keep-Alive',
    'Accept-Encoding': 'gzip',
    'Content-Type': 'application/octet-stream',
    'X-Unity-Version': '2018.4.12f1',
    'X-GA-SV': '1789535859',
    'X-GA': 'v1 1',
    'ReleaseVersion': 'OB55',
}


# ==================== TOKEN CACHE ====================
def _json_serializer(obj):
    if isinstance(obj, (bytes, bytearray)):
        return {"__bytes_hex__": bytes(obj).hex()}
    raise TypeError(f"Type {type(obj)} not serializable")


def _json_deserializer(obj):
    if isinstance(obj, dict):
        if "__bytes_hex__" in obj and len(obj) == 1:
            try:
                return bytes.fromhex(obj["__bytes_hex__"])
            except Exception:
                return b""
        return {k: _json_deserializer(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_deserializer(x) for x in obj]
    return obj


_TOKEN_CACHE: Optional[Dict[str, Any]] = None
_TOKEN_CACHE_LOCK = asyncio.Lock()


def _load_token_cache_sync() -> Dict[str, Any]:
    if not os.path.exists(TOKEN_CACHE_FILE):
        return {}
    try:
        with open(TOKEN_CACHE_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return {}
        data = json.loads(content)
        if not isinstance(data, dict):
            return {}
        return _json_deserializer(data)
    except Exception:
        return {}


def _save_token_cache_sync(cache: Dict[str, Any]):
    try:
        tmp = TOKEN_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, default=_json_serializer)
        os.replace(tmp, TOKEN_CACHE_FILE)
    except Exception:
        pass


async def cache_get(uid: str) -> Optional[Dict]:
    global _TOKEN_CACHE
    async with _TOKEN_CACHE_LOCK:
        if _TOKEN_CACHE is None:
            loop = asyncio.get_running_loop()
            _TOKEN_CACHE = await loop.run_in_executor(None, _load_token_cache_sync)

        entry = _TOKEN_CACHE.get(str(uid))
        if not entry:
            return None
        if time.time() - entry.get("cached_at", 0) > TOKEN_CACHE_TTL:
            _TOKEN_CACHE.pop(str(uid), None)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _save_token_cache_sync, dict(_TOKEN_CACHE))
            return None
        if str(entry.get("account_id", "")).isdigit():
            entry["account_id"] = int(entry["account_id"])
        return entry


async def cache_set(uid: str, account_data: Dict):
    global _TOKEN_CACHE
    async with _TOKEN_CACHE_LOCK:
        if _TOKEN_CACHE is None:
            _TOKEN_CACHE = {}
        entry = dict(account_data)
        entry["cached_at"] = time.time()
        _TOKEN_CACHE[str(uid)] = entry
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _save_token_cache_sync, dict(_TOKEN_CACHE))


# ==================== ENCRYPTION ====================
async def aes_encrypt(payload: bytes, key: bytes, iv: bytes) -> bytes:
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return cipher.encrypt(pad(payload, AES.block_size))


_VERSION_CONFIG_CACHE = None
_VERSION_CONFIG_CACHE_TIME = 0.0
_VERSION_CONFIG_TTL = 1800.0
_VERSION_LOCK = asyncio.Lock()


async def get_playstore_version():
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: play_scraper('com.dts.freefireth', lang='hi', country='id')
        )
        return result.get("version")
    except Exception:
        return "1.132.9"


async def version_config(region: str = "IND"):
    global _VERSION_CONFIG_CACHE, _VERSION_CONFIG_CACHE_TIME
    now = time.time()
    if _VERSION_CONFIG_CACHE and (now - _VERSION_CONFIG_CACHE_TIME) < _VERSION_CONFIG_TTL:
        return _VERSION_CONFIG_CACHE

    async with _VERSION_LOCK:
        now = time.time()
        if _VERSION_CONFIG_CACHE and (now - _VERSION_CONFIG_CACHE_TIME) < _VERSION_CONFIG_TTL:
            return _VERSION_CONFIG_CACHE

        reg = str(region).upper() if region else "IND"
        regional_url = REGIONAL_LOGIN_HOSTS.get(reg, REGIONAL_LOGIN_HOSTS["IND"])
        FALLBACK = ("OB55", "1.132.9", regional_url)

        try:
            app_version, _ = await asyncio.gather(
                get_playstore_version(),
                asyncio.sleep(0),
            )
            app_version = app_version or "1.132.9"
            api_url = (
                "https://version.ggwhitehawk.com/live/ver.php"
                f"?version={app_version}"
                "&lang=hi&device=android&channel=android"
                "&appstore=googleplay&region=IND"
                "&whitelist_version=1.3.0&whitelist_sp_version=1.0.0"
            )
            response = await client.get(api_url, timeout=6.0)
            if response.status_code == 200:
                data = response.json()
                server_url = data.get("server_url") or regional_url
                remote_version = data.get("remote_version")
                latest_release_version = data.get("latest_release_version")
                if server_url and remote_version and latest_release_version:
                    _VERSION_CONFIG_CACHE = (latest_release_version, remote_version, server_url)
                    _VERSION_CONFIG_CACHE_TIME = now
                    return _VERSION_CONFIG_CACHE
        except Exception:
            pass

        _VERSION_CONFIG_CACHE = FALLBACK
        _VERSION_CONFIG_CACHE_TIME = now
        return _VERSION_CONFIG_CACHE


async def get_access_token(uid, password):
    url = "https://100067.connect.garena.com/oauth/guest/token/grant"
    hdrs = {
        "Host": "100067.connect.garena.com",
        "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 12; SM-G998B Build/SP1A.210812.016)",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "close",
    }
    data = {
        "uid": uid,
        "password": password,
        "response_type": "token",
        "client_type": "2",
        "client_secret": "2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3",
        "client_id": "100067",
    }
    for attempt in range(3):
        try:
            response = await client.post(url, headers=hdrs, data=data)
            if response.status_code == 200:
                rd = response.json()
                open_id = rd.get("open_id")
                access_token = rd.get("access_token")
                platform = rd.get("platform", 4)
                if open_id and access_token:
                    return open_id, access_token, platform
            if response.status_code == 429:
                await asyncio.sleep(0.8)
                continue
        except Exception:
            pass
        await asyncio.sleep(0.3)
    return None


# ==================== MANUAL PROTOBUF ====================
def encode_varint(n: int) -> bytes:
    if n < 0:
        return b''
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            b |= 0x80
        out.append(b)
        if not n:
            break
    return bytes(out)


def build_proto(fields: dict) -> bytes:
    parts = []
    for k, v in fields.items():
        if isinstance(v, dict):
            nested = build_proto(v)
            parts.append(encode_varint((k << 3) | 2) + encode_varint(len(nested)) + nested)
        elif isinstance(v, int):
            parts.append(encode_varint((k << 3) | 0) + encode_varint(v))
        elif isinstance(v, (str, bytes)):
            ev = v.encode() if isinstance(v, str) else v
            parts.append(encode_varint((k << 3) | 2) + encode_varint(len(ev)) + ev)
    return b''.join(parts)


def encrypt_api_bytes(plain_bytes: bytes) -> bytes:
    return AES.new(AES_KEY, AES.MODE_CBC, AES_IV).encrypt(
        pad(plain_bytes, AES.block_size)
    )


async def build_majorlogin_payload(open_id, access_token, platform, client_version, device_info):
    try:
        payload_dict = {
            3:  str(datetime.now())[:-7],
            4:  "free fire",
            5:  "1",
            7:  str(client_version) if client_version else "1.132.8",
            8:  str(device_info.get("system_software", "Android OS 15 / API-35")),
            9:  "Handheld",
            10: "Telkomsel",
            11: "CarrierDataNetwork",
            12: str(device_info.get("screen_width", 1639)),
            13: str(device_info.get("screen_height", 720)),
            14: str(device_info.get("screen_dpi", 320)),
            15: str(device_info.get("processor_details", "ARM64 FP ASIMD AES | 2000 | 8")),
            16: str(device_info.get("memory", 5653)),
            17: str(device_info.get("gpu_renderer", "Mali-G52 MC2")),
            18: "OpenGL ES 3.2 v1.r49p1-03bet0.19498e0ae1d5dac223383c39a2e58f04",
            19: str(device_info.get("unique_device_id", f"Google|{uuid.uuid4()}")),
            20: str(device_info.get("client_ip", "182.5.135.225")),
            21: "en",
            22: str(open_id),
            23: str(platform),
            24: "Handheld",
            25: str(device_info.get("model", "Xiaomi 2409BRN2CY")),
            26: "ID",
            29: str(access_token),
            30: "1",
            41: "Telkomsel",
            42: "4G",
            57: "7428b253defc164018c604a1ebbfebdf",
            60: "109278", 61: "55402", 62: "868", 64: "55927",
            65: "109278", 66: "55927", 67: "109278", 73: "2",
            74: "/data/app/~~6Cw028g_ld-8Z0z2qhrOLA==/com.dts.freefireth-i3wiaEmpx8CbiRVhUMx_ag==/lib/arm64",
            76: "1",
            77: "b8e0cd5e295eee42f5860d3c86e483dd|/data/app/~~6Cw028g_ld-8Z0z2qhrOLA==/com.dts.freefireth-i3wiaEmpx8CbiRVhUMx_ag==/base.apk",
            78: "3", 79: "2", 81: "64", 83: "2019121229",
            85: "3", 86: "OpenGLES2", 87: "8191", 88: str(platform),
            90: "Cirebon", 91: "JB", 92: "14466", 93: "android",
            94: "KqsHTyb7yHjsAiyNWUUZa0DSOD6U0AhDQ76smAo1WeHHHpSmJ8KMRyRrxb2ttgJqO9YOqO/K6AORAvDMAX/qBS8+JahIkrM7kHfzlfULbgRAoZ9r",
            95: "111207",
            96: "{\"cur_rate\":[60,90,120],\"support_etc2\":false}",
            97: "1", 98: "1", 99: str(platform), 100: str(platform),
            102: "D]C\x15\x05T\x0eU3", 103: "1", 104: "52837", 105: "1",
            106: "https://dl.gmc.freefiremobile.com/live/ABHotUpdates/|https://core-gmc.freefiremobile.com/live/ABHotUpdates/|211c933168f55902c7dfbfd8c4e2957d",
            107: "1.3fb8adbf45f44ce9",
        }
        raw_proto = build_proto(payload_dict)
        return encrypt_api_bytes(raw_proto)
    except Exception:
        return None
async def _try_majorlogin(base: str, data: bytes, release_version: str):
    url = f"{base.rstrip('/')}/MajorLogin"
    req_headers = _BASE_HEADERS.copy()
    req_headers["ReleaseVersion"] = release_version
    req_headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        response = await client.post(url, headers=req_headers, content=bytes(data))
        if response.status_code != 200:
            return None
        rc = response.content
        if len(rc) < 20:
            return None

        try:
            res_proto = MASTERTeam_pb2.MajorLoginRes()
            res_proto.ParseFromString(rc)
            if res_proto.region and res_proto.token:
                return res_proto
        except Exception:
            pass

        if len(rc) > 64:
            try:
                res_proto = MASTERTeam_pb2.MajorLoginRes()
                res_proto.ParseFromString(rc[64:])
                if res_proto.region and res_proto.token:
                    return res_proto
            except Exception:
                pass

        for offset in range(min(160, len(rc))):
            try:
                candidate = MASTERTeam_pb2.MajorLoginRes()
                candidate.ParseFromString(rc[offset:])
                if candidate.region and candidate.token:
                    return candidate
            except Exception:
                continue
    except Exception:
        return None
    return None


async def send_majorlogin(data: bytes, release_version: str, server_url: Optional[str]):
    if not data:
        return None

    candidates: List[str] = []
    if server_url:
        candidates.append(server_url.rstrip('/') + "/")
    for host in MAJORLOGIN_FALLBACK_HOSTS:
        normalized = host.rstrip('/') + "/"
        if normalized not in candidates:
            candidates.append(normalized)

    tasks = [asyncio.create_task(_try_majorlogin(base, data, release_version)) for base in candidates]
    try:
        done, pending = await asyncio.wait(
            tasks, timeout=HOST_RACE_TIMEOUT, return_when=asyncio.FIRST_COMPLETED
        )
        for t in done:
            try:
                res = t.result()
                if res is not None:
                    for p in pending:
                        p.cancel()
                    return res
            except Exception:
                continue

        for t in pending:
            try:
                res = await t
                if res is not None:
                    return res
            except Exception:
                continue
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
    return None


async def _try_getlogin(base: str, data: bytes, token: str, release_version: str):
    url = f"{base.rstrip('/')}/GetLoginData"
    req_headers = _BASE_HEADERS.copy()
    req_headers["ReleaseVersion"] = release_version
    req_headers["Authorization"] = f"Bearer {token}"
    req_headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        from urllib.parse import urlparse
        req_headers["Host"] = urlparse(base).netloc
    except Exception:
        pass

    try:
        response = await client.post(url, headers=req_headers, content=bytes(data))
        if response.status_code != 200:
            return None
        rc = response.content

        res_proto = MASTERTeam_pb2.GetLoginDataRes()
        parsed = False
        try:
            res_proto.ParseFromString(rc)
            if res_proto.functional_addrs or res_proto.informational_addrs:
                parsed = True
        except Exception:
            pass

        if not parsed:
            for offset in range(min(160, len(rc))):
                try:
                    cand = MASTERTeam_pb2.GetLoginDataRes()
                    cand.ParseFromString(rc[offset:])
                    if cand.functional_addrs or cand.informational_addrs:
                        res_proto = cand
                        parsed = True
                        break
                except Exception:
                    continue

        if not parsed:
            return None

        dict_res = {}
        try:
            for r in Parser().parse(rc.hex()):
                key = str(r.field)
                if r.wire_type in ("varint", "string", "bytes"):
                    dict_res[key] = {"data": r.data}
        except Exception:
            pass

        return res_proto, dict_res
    except Exception:
        return None


async def send_getlogin(data: bytes, base_url: Optional[str], token: str, release_version: str, region: str = "IND"):
    if not data:
        return None

    reg = str(region).upper() if region else "IND"
    candidates: List[str] = []

    regional = REGIONAL_LOGIN_HOSTS.get(reg)
    if regional:
        candidates.append(regional.rstrip('/') + "/")
    if base_url:
        candidates.append(base_url.rstrip('/') + "/")
    for host in MAJORLOGIN_FALLBACK_HOSTS:
        normalized = host.rstrip('/') + "/"
        if normalized not in candidates:
            candidates.append(normalized)

    tasks = [asyncio.create_task(_try_getlogin(base, data, token, release_version)) for base in candidates]
    try:
        done, pending = await asyncio.wait(
            tasks, timeout=HOST_RACE_TIMEOUT, return_when=asyncio.FIRST_COMPLETED
        )
        for t in done:
            try:
                res = t.result()
                if res is not None:
                    for p in pending:
                        p.cancel()
                    return res
            except Exception:
                continue
        for t in pending:
            try:
                res = await t
                if res is not None:
                    return res
            except Exception:
                continue
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
    return None


async def build_tcp_startup_packet(account_id, token, server_time, key, iv, region="BD", typ='OnLine'):
    uid_hex = f"{int(account_id):016x}"
    timestamp_hex = f"{int(server_time):08x}"
    encode_token = token.encode()
    encrypted_packet = (await aes_encrypt(encode_token, key, iv)).hex()
    encrypted_packet_length = f"{len(encrypted_packet) // 2:08x}"
    reg = str(region).upper() if region else "BD"
    if typ == 'OnLine':
        prefix = '7219' if reg == 'BD' else ('7214' if reg == 'IND' else '7215')
        return f"{prefix}{uid_hex}{timestamp_hex}00000000{encrypted_packet_length}{encrypted_packet}"
    else:
        prefix = '8119' if reg == 'BD' else ('8114' if reg == 'IND' else '8115')
        return f"{prefix}{uid_hex}{timestamp_hex}{encrypted_packet_length}{encrypted_packet}"


_KEEPALIVE_CACHE: Dict[str, bytes] = {}


async def send_keep_alive(region="BD") -> bytes:
    reg = str(region).upper() if region else "BD"
    if reg in _KEEPALIVE_CACHE:
        return _KEEPALIVE_CACHE[reg]
    ka_hex = "0219" if reg == "BD" else ("0214" if reg == "IND" else "0215")
    try:
        data = bytes.fromhex(ka_hex)
    except Exception:
        data = bytes.fromhex("0219")
    _KEEPALIVE_CACHE[reg] = data
    return data


def get_proto_field(d, key, default=None):
    if not d or not isinstance(d, dict):
        return default
    if key in d:
        val = d[key].get('data')
        return val if val is not None else default
    if str(key) in d:
        val = d[str(key)].get('data')
        return val if val is not None else default
    return default


# ==================== LOGIN FLOW ====================
async def _login_account(account: Dict) -> Optional[Dict]:
    uid_input = account.get("uid", "")
    password = account.get("password", "")
    token_input = account.get("token", "")

    if token_input:
        cached = await cache_get(f"tok_{token_input[:20]}")
        if cached:
            return cached

        async with _LOGIN_SEMAPHORE:
            release_version, client_version, server_url = await version_config(region="IND")

            url = f"https://100067.connect.garena.com/oauth/token/inspect?token={token_input}"
            hdrs = {
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "close",
                "Content-Type": "application/x-www-form-urlencoded",
                "Host": "100067.connect.garena.com",
                "User-Agent": "GarenaMSDK/4.0.19P4(G011A ;Android 9;en;US;)",
            }
            resp = await client.get(url, headers=hdrs, timeout=8.0)
            if resp.status_code != 200:
                raise Exception("token inspect failed")
            data = resp.json()
            if 'error' in data:
                raise Exception("invalid token")
            open_id = data.get('open_id')
            platform = data.get('platform', 4)
            if not open_id:
                raise Exception("no open_id")

            device_info = await get_device_for_account(open_id)
            login_payload_data = await build_majorlogin_payload(
                open_id, token_input, str(platform), client_version, device_info
            )
            majorlogin = await send_majorlogin(login_payload_data, release_version, server_url)
            if majorlogin is None:
                raise Exception("MajorLogin failed")
            getlogin = await send_getlogin(
                login_payload_data, majorlogin.url, majorlogin.token,
                release_version, region=majorlogin.region or "IND"
            )
            if getlogin is None:
                raise Exception("GetLoginData failed")
            res_proto, dict_res = getlogin

            level = int(get_proto_field(dict_res, 6, 1))
            nickname = res_proto.nickname or get_proto_field(dict_res, 4, f"Player_{majorlogin.account_id}")
            region = majorlogin.region or get_proto_field(dict_res, 3, "BD")

            account_data = {
                'account_id': majorlogin.account_id,
                'nickname': nickname,
                'region': region,
                'level': level,
                'token': majorlogin.token,
                'server_time': majorlogin.server_time,
                'aes_ak': majorlogin.aes_ak,
                'iv_i': majorlogin.iv_i,
                'functional_addrs': res_proto.functional_addrs,
                'informational_addrs': res_proto.informational_addrs,
                'release_version': release_version,
                'client_version': client_version,
                'server_url': majorlogin.url,
                'login_payload_data': login_payload_data,
                'auth_type': 'token',
                'auth_token': token_input,
            }
            await cache_set(f"tok_{token_input[:20]}", account_data)
            return account_data

    cached = await cache_get(str(uid_input))
    if cached:
        return cached

    async with _LOGIN_SEMAPHORE:
        release_version, client_version, server_url = await version_config(region="IND")

        tokengrant = await get_access_token(uid_input, password)
        if tokengrant is None:
            raise Exception("token grant failed")
        open_id, access_token, platform = tokengrant

        device_info = await get_device_for_account(uid_input)
        login_payload_data = await build_majorlogin_payload(
            open_id, access_token, platform, client_version, device_info
        )
        majorlogin = await send_majorlogin(login_payload_data, release_version, server_url)
        if majorlogin is None:
            raise Exception("MajorLogin failed")
        getlogin = await send_getlogin(
            login_payload_data, majorlogin.url, majorlogin.token,
            release_version, region=majorlogin.region or "IND"
        )
        if getlogin is None:
            raise Exception("GetLoginData failed")
        res_proto, dict_res = getlogin

        level = int(get_proto_field(dict_res, 6, 1))
        nickname = res_proto.nickname or get_proto_field(dict_res, 4, f"Player_{majorlogin.account_id}")
        region = majorlogin.region or get_proto_field(dict_res, 3, "BD")

        account_data = {
            'account_id': majorlogin.account_id,
            'nickname': nickname,
            'region': region,
            'level': level,
            'open_id': open_id,
            'access_token': access_token,
            'platform': str(platform),
            'token': majorlogin.token,
            'server_time': majorlogin.server_time,
            'aes_ak': majorlogin.aes_ak,
            'iv_i': majorlogin.iv_i,
            'functional_addrs': res_proto.functional_addrs,
            'informational_addrs': res_proto.informational_addrs,
            'release_version': release_version,
            'client_version': client_version,
            'server_url': majorlogin.url,
            'login_payload_data': login_payload_data,
            'auth_type': 'guest',
            'auth_uid': uid_input,
            'auth_password': password,
        }
        await cache_set(str(uid_input), account_data)
        return account_data


async def activate_single_account(
    account: Dict,
    account_index: int,
    total: int,
    results: Dict,
    results_lock: asyncio.Lock,
):
    uid_input = account.get("uid", "")
    password = account.get("password", "")
    token_input = account.get("token", "")
    tag = f"[{account_index}/{total}]"

    try:
        account_data = await _login_account(account)

        acc_id = str(account_data['account_id'])
        nick = account_data.get('nickname', f"Player_{acc_id}")
        lvl = account_data.get('level', 1)
        reg = account_data.get('region', 'BD')

        tcp_packet = await build_tcp_startup_packet(
            account_data['account_id'],
            account_data['token'],
            account_data['server_time'],
            account_data['aes_ak'],
            account_data['iv_i'],
            region=reg,
            typ='OnLine',
        )

        addrs = account_data['functional_addrs']
        ip, port = addrs.split(":")
        resolved_ip = await resolve_host(ip)

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(resolved_ip, int(port)),
            timeout=TCP_CONNECT_TIMEOUT,
        )
        raw_sock = writer.get_extra_info('socket')
        if raw_sock:
            optimize_tcp_socket(raw_sock)

        writer.write(bytes.fromhex(tcp_packet))
        await writer.drain()

        try:
            ka = await send_keep_alive(reg)
            if ka and writer and not writer.is_closing():
                writer.write(ka)
                await asyncio.wait_for(writer.drain(), timeout=1.5)
        except Exception:
            pass

        print(f"[{_ts()}] [ONLINE] {tag} id={acc_id} nick={nick} lvl={lvl} region={reg}")

        start_time = time.time()
        keepalive = await send_keep_alive(reg)
        while time.time() - start_time < ONLINE_DURATION:
            try:
                data = await asyncio.wait_for(reader.read(4096), timeout=0.3)
                if not data:
                    break
            except asyncio.TimeoutError:
                try:
                    if writer and not writer.is_closing():
                        writer.write(keepalive)
                        await writer.drain()
                except Exception:
                    break
            except Exception:
                break

        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=0.8)
        except Exception:
            pass

        async with results_lock:
            results["active"].append({
                "uid": acc_id,
                "game_id": str(uid_input) if uid_input else "",
                "password": password if password else "N/A",
                "input_uid": str(uid_input) if uid_input else "",
                "nickname": nick,
                "level": lvl,
                "region": reg,
                "activated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
        return True

    except Exception as e:
        async with results_lock:
            results["failed"].append({
                "uid": str(uid_input) if uid_input else (token_input[:20] if token_input else "unknown"),
                "reason": str(e)[:100],
            })
        print(f"[{_ts()}] [FAILED] {tag} {uid_input or token_input[:16]} reason={str(e)[:60]}")
        return False


async def process_batch(batch: List[Dict], start_idx: int, results: Dict, results_lock: asyncio.Lock):
    total = len(batch)
    tasks = [
        activate_single_account(acc, start_idx + i + 1, total, results, results_lock)
        for i, acc in enumerate(batch)
    ]
    await asyncio.gather(*tasks, return_exceptions=True)


# ==================== JSON LOADERS ====================
def find_json_files(search_path: str) -> List[str]:
    if not search_path:
        search_path = "."
    if os.path.isfile(search_path) and search_path.endswith(".json"):
        return [search_path]
    if not os.path.isdir(search_path):
        return []
    try:
        return [
            os.path.join(search_path, f)
            for f in os.listdir(search_path)
            if f.endswith(".json")
        ]
    except Exception:
        return []


def load_accounts_from_json(json_path: str) -> List[Dict]:
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print_error(f"Failed to load JSON: {e}")
        return []

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if "accounts" in data and isinstance(data["accounts"], list):
            return data["accounts"]
        if "uid" in data or "token" in data:
            return [data]
        accounts = []
        for k, v in data.items():
            if isinstance(v, str):
                accounts.append({"uid": k, "password": v})
            elif isinstance(v, dict):
                acc = dict(v)
                acc.setdefault("uid", k)
                accounts.append(acc)
        return accounts
    return []


# ==================== OUTPUT ====================
def save_active_output(results: Dict):
    output = {
        "summary": {
            "total_active": len(results["active"]),
            "total_failed": len(results["failed"]),
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
        "active_accounts": [
            {
                "uid": a.get("uid", ""),
                "game_id": a.get("game_id", ""),
                "password": a.get("password", ""),
                "input_uid": a.get("input_uid", ""),
                "nickname": a.get("nickname", ""),
                "level": a.get("level", 1),
                "region": a.get("region", ""),
                "activated_at": a.get("activated_at", ""),
            }
            for a in results["active"]
        ],
        "failed_accounts": results["failed"],
    }
    try:
        with open(ACTIVE_OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        print_success(f"Saved output -> {ACTIVE_OUTPUT_FILE}")
    except Exception as e:
        print_error(f"Failed to save output: {e}")


# ==================== BANNER ====================
def print_banner():
    print()
    print("=" * 58)
    print("           CHINNU  -  ACCOUNT  ACTIVATOR")
    print("             PREMIUM EDITION  -  v2.0")
    print("=" * 58)
    print()


# ==================== SUMMARY ====================
def print_summary(results: Dict, elapsed: float, total: int):
    active_count = len(results["active"])
    failed_count = len(results["failed"])
    rate = (active_count / total * 100) if total else 0

    print()
    print("=" * 58)
    print("  ACTIVATION SUMMARY")
    print("=" * 58)
    print(f"  Total Accounts : {total}")
    print(f"  Active         : {active_count}")
    print(f"  Failed         : {failed_count}")
    print(f"  Time Taken     : {elapsed:.2f}s")
    print(f"  Success Rate   : {rate:.1f}%")
    print("=" * 58)
    print()

    if active_count > 0:
        print("  ACTIVE ACCOUNTS")
        print("  " + "-" * 58)
        for i, acc in enumerate(results["active"], 1):
            print(
                f"  {i:>3}. uid={acc['uid']:<12} nick={acc['nickname']:<20} "
                f"lvl={acc['level']} region={acc['region']}"
            )
        print()

    if failed_count > 0:
        print("  FAILED ACCOUNTS")
        print("  " + "-" * 58)
        for i, acc in enumerate(results["failed"][:20], 1):
            print(f"  {i:>3}. {acc['uid']:<20} -- {acc['reason']}")
        if failed_count > 20:
            print(f"  ... and {failed_count - 20} more")
        print()


# ==================== CLI FLOW ====================
async def run_activator(json_path: str):
    accounts = load_accounts_from_json(json_path)
    if not accounts:
        print_error("No accounts found in JSON file.")
        return

    print_success(f"Loaded {len(accounts)} accounts from: {json_path}")
    print_info(f"Batch size: {BATCH_SIZE} / interval: {BATCH_INTERVAL}s")
    print_info(f"Online duration: {ONLINE_DURATION}s per account")
    print_info(f"Concurrency: {LOGIN_CONCURRENCY}")
    print()

    results = {"active": [], "failed": []}
    results_lock = asyncio.Lock()

    start_time = time.time()
    total = len(accounts)

    for i in range(0, total, BATCH_SIZE):
        batch = accounts[i:i + BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        print(f"--- BATCH {batch_num} ({len(batch)} accounts) ---")
        batch_start = time.time()
        await process_batch(batch, i, results, results_lock)
        batch_elapsed = time.time() - batch_start
        sleep_time = max(0, BATCH_INTERVAL - batch_elapsed)
        if sleep_time > 0:
            await asyncio.sleep(sleep_time)

    elapsed = time.time() - start_time
    print_summary(results, elapsed, total)
    save_active_output(results)


async def main():
    print_banner()
    try:
        json_path_input = input("Enter JSON path: ").strip()
    except (EOFError, KeyboardInterrupt):
        print_warning("Cancelled.")
        return
    if not json_path_input:
        print_error("No path provided.")
        return
    json_files = find_json_files(json_path_input)
    if not json_files:
        print_error(f"No JSON files at: {json_path_input}")
        return
    for i, f in enumerate(json_files, 1):
        try:
            size = os.path.getsize(f)
        except Exception:
            size = 0
        print(f"  [{i}] {f} ({size} bytes)")
    try:
        choice = input(f"Select file (1-{len(json_files)}): ").strip()
        idx = int(choice) - 1
        if idx < 0 or idx >= len(json_files):
            print_error("Invalid selection.")
            return
    except Exception:
        print_error("Invalid input.")
        return
    await run_activator(json_files[idx])


# ==================== SHUTDOWN HOOK ====================
async def close_client():
    try:
        await client.aclose()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print_warning("Stopped.")