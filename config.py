import os
import glob
from typing import List, Optional

# ==========================================
# 1. BASE & RAILWAY DEPLOYMENT CONFIG
# ==========================================
PORT = int(os.environ.get("PORT", 8000))
HOST = os.environ.get("HOST", "0.0.0.0")

# Auto-detect public URL from Railway, Render, Koyeb or manual PUBLIC_URL
def _detect_public_url() -> Optional[str]:
    # 1. Manual override always wins
    manual = os.environ.get("PUBLIC_URL")
    if manual:
        return manual.rstrip("/")
    # 2. Railway: RAILWAY_PUBLIC_DOMAIN gives just the domain (no scheme)
    rail = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if rail:
        return f"https://{rail}"
    # 3. Render: RENDER_EXTERNAL_URL already includes https://
    render = os.environ.get("RENDER_EXTERNAL_URL")
    if render:
        return render.rstrip("/")
    # 4. Koyeb: KOYEB_PUBLIC_DOMAIN gives just the domain
    koyeb = os.environ.get("KOYEB_PUBLIC_DOMAIN")
    if koyeb:
        return f"https://{koyeb}"
    return None

PUBLIC_URL = _detect_public_url()

# ==========================================
# 2. MONGODB CACHING CONFIGURATION
# ==========================================
MONGO_URI = os.environ.get(
    "MONGO_URI",
    "mongodb+srv://usaomega1_db_user:Masdt2qYlJfXs20N@cluster0.zpedlcp.mongodb.net/?appName=Cluster0"
)
MONGO_DB_NAME = os.environ.get("MONGO_DB_NAME", "yt_streams_cache")
MONGO_COLLECTION_NAME = os.environ.get("MONGO_COLLECTION_NAME", "media_cache")

# Cache TTL: Time To Live before re-extracting expired YouTube CDN links (in seconds, default 5 hours)
# YouTube links typically expire after 6 hours.
CACHE_EXPIRY_SECONDS = int(os.environ.get("CACHE_EXPIRY_SECONDS", 18000))

# ==========================================
# 3. PROXY ROTATION & FAILOVER SYSTEM
# ==========================================
DEFAULT_PROXY = "socks5://03wwyzpv5xek:nzjfrugmo40zp4d@65.111.10.198:1081"
PROXY_FILE = os.environ.get("PROXY_FILE", os.path.join(os.path.dirname(__file__), "proxy.txt"))

def load_proxy_pool() -> List[str]:
    """
    Loads all proxies from proxy.txt and environment variables.
    Supports formats:
      - socks5://user:pass@host:port
      - http://user:pass@host:port
      - host:port:user:pass (auto-detects socks vs http)
      - host:port
    """
    proxies = []
    
    # 1. First priority: proxy.txt file
    if os.path.exists(PROXY_FILE):
        try:
            with open(PROXY_FILE, "r", encoding="utf-8") as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue
                    # Remove trailing slashes or quotes
                    line = line.strip("'\"")
                    # If line has scheme (socks5:// or http://)
                    if line.startswith("socks5://") or line.startswith("socks4://") or line.startswith("http://") or line.startswith("https://"):
                        if line not in proxies:
                            proxies.append(line)
                    # Parse host:port:user:pass
                    elif line.count(":") == 3:
                        parts = line.split(":")
                        formatted = f"socks5://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
                        if formatted not in proxies:
                            proxies.append(formatted)
                    # Parse host:port
                    elif line.count(":") == 1:
                        formatted = f"http://{line}"
                        if formatted not in proxies:
                            proxies.append(formatted)
                    elif line not in proxies:
                        proxies.append(line)
        except Exception as e:
            print(f"[CONFIG] Error reading proxy file {PROXY_FILE}: {e}")

    # 2. Environment variable PROXY_URL
    env_proxy = os.environ.get("PROXY_URL")
    if env_proxy and env_proxy.strip():
        ep = env_proxy.strip().strip("'\"")
        if ep not in proxies:
            proxies.insert(0, ep)

    # 3. Default fallback proxy
    if DEFAULT_PROXY and DEFAULT_PROXY not in proxies:
        proxies.append(DEFAULT_PROXY)

    return proxies

# ==========================================
# 4. COOKIE ROTATION & AUTO-ADJUST SYSTEM
# ==========================================
# Auto discovers cookies.txt, cookies1.txt, cookies2.txt, etc.
def load_cookie_pool() -> List[str]:
    """
    Finds all valid cookie files in the workspace (cookies.txt, cookies1.txt, etc.)
    """
    pool = []
    base_dir = os.path.dirname(__file__)
    
    # 1. Check primary cookies.txt
    primary = os.path.join(base_dir, "cookies.txt")
    if os.path.exists(primary) and os.path.getsize(primary) > 10:
        pool.append(primary)

    # 2. Check numbered cookies*.txt
    for match in sorted(glob.glob(os.path.join(base_dir, "cookies*.txt"))):
        if match not in pool and os.path.getsize(match) > 10:
            pool.append(match)

    return pool
