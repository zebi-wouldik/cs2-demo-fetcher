#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CS2 Demo Downloader
Downloads demos via Steam API (auth code, like Leetify).
CSDM naming: match730_MATCHID_RESERVATIONID.dem
"""

import bz2
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import threading
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Callable

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# ════════════════════════════════════════════════════════════════
#  PATHS
# ════════════════════════════════════════════════════════════════

SCRIPT_DIR  = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
BOILER_DIR  = SCRIPT_DIR / "boiler"

# ════════════════════════════════════════════════════════════════
#  CONSTANTS
# ════════════════════════════════════════════════════════════════

STEAM_NEXT_CODE_URL = (
    "https://api.steampowered.com"
    "/ICSGOPlayers_730/GetNextMatchSharingCode/v1"
)
GITHUB_BOILER_API = (
    "https://api.github.com/repos/akiver/boiler-writter/releases/latest"
)

SHARECODE_ALPHABET = "ABCDEFGHJKLMNOPQRSTUVWXYZabcdefhijkmnopqrstuvwxyz23456789"
SHARECODE_BASE     = len(SHARECODE_ALPHABET)

DOWNLOAD_WORKERS  = 4
# Max observed ID difference between the SAME match downloaded by two different tools.
# Consecutive DIFFERENT matches always differ by more than this.
_SAME_MATCH_THRESHOLD = 6_000_000_000_000   # 6 trillion

BOILER_DELAY      = 4
API_RATE_DELAY    = 0.4
MAX_RETRIES       = 3
RETRY_BACKOFF     = 2
HTTP_TIMEOUT      = 15
DOWNLOAD_TIMEOUT  = 180
BOILER_TIMEOUT    = 45
CHUNK_SIZE        = 1 << 16

BOILER_EXIT_CODES = {
    0: "OK", 1: "Invalid arguments", 2: "Steam needs restart",
    3: "Steam not running", 4: "User not logged in / GC busy",
    5: "CS2 not installed", 6: "Game Coordinator connection error",
    7: "GC timeout", 8: "Match not found (expired > 30 days)",
}
BOILER_PERMANENT_FAILURES = {1, 5, 8}

PLATFORM_ASSETS = {
    ("Windows","AMD64"):  {"kw":["win"],         "exc":["mac","linux"],"bin":"boiler-writter.exe","ext":".zip"},
    ("Windows","ARM64"):  {"kw":["win"],         "exc":["mac","linux"],"bin":"boiler-writter.exe","ext":".zip"},
    ("Linux","x86_64"):   {"kw":["linux"],       "exc":["mac","win"],  "bin":"boiler-writter",    "ext":".zip"},
    ("Linux","aarch64"):  {"kw":["linux"],       "exc":["mac","win"],  "bin":"boiler-writter",    "ext":".zip"},
    ("Darwin","x86_64"):  {"kw":["mac"],         "exc":["arm64","win"],"bin":"boiler-writter",    "ext":".zip"},
    ("Darwin","arm64"):   {"kw":["mac","arm64"], "exc":["win"],        "bin":"boiler-writter",    "ext":".zip"},
}

_RE_STEAMID      = re.compile(r'^\d{17}$')
_RE_MID          = re.compile(r'(?:match730_)?(0*\d{16,21})[_\-]\d')
_RE_DEMO_URL     = re.compile(r'https?://[^\x00-\x1f\x7f\s]{15,}\.dem(?:\.bz2)?')
_RE_FALLBACK_URL = re.compile(r'https?://[^\x00-\x1f\x7f\s]{15,}')
_BZ2_MAGIC       = b'BZ'


# ════════════════════════════════════════════════════════════════
#  THREAD-SAFE UTILS
# ════════════════════════════════════════════════════════════════

class _SafeSet:
    __slots__ = ('_data', '_ints', '_lock')

    def __init__(self, initial=None):
        self._data = set()
        self._ints = set()   # integer versions for proximity check
        self._lock = threading.Lock()
        if initial:
            for item in initial:
                self._data.add(item)
                try:
                    self._ints.add(int(item))
                except (ValueError, TypeError):
                    pass

    def add(self, item):
        with self._lock:
            self._data.add(item)
            try:
                self._ints.add(int(item))
            except (ValueError, TypeError):
                pass

    def __contains__(self, item):
        with self._lock:
            # 1. Exact match (fast path)
            if item in self._data:
                return True
            # 2. Proximity match — same match downloaded by a different tool
            #    produces an ID up to ~5T different. Consecutive different
            #    matches always differ by >7T, so 6T threshold is safe.
            try:
                v = int(item)
                return any(
                    abs(v - x) <= _SAME_MATCH_THRESHOLD
                    for x in self._ints
                )
            except (ValueError, TypeError):
                return False

    def __len__(self):
        with self._lock: return len(self._data)

_print_lock = threading.Lock()
def _safe_print(*args, **kwargs):
    with _print_lock: print(*args, **kwargs)


# ════════════════════════════════════════════════════════════════
#  PROGRESS — compatible with all terminals
# ════════════════════════════════════════════════════════════════

class _ProgressDisplay:
    """
    Terminal-safe display (no ANSI cursor-up).
    One \\r line for live summary, permanent lines for completions.
    """

    def __init__(self, total_count: int):
        self._lock      = threading.Lock()
        self._total     = total_count
        self._done      = 0
        self._failed    = 0
        self._active: Dict[str, Tuple[int, int]] = {}
        self._errors: List[Tuple[str, str]] = []

    def assign(self, name: str):
        with self._lock:
            self._active[name] = (0, 0)
            self._draw()

    def update(self, name: str, downloaded: int, total: int):
        with self._lock:
            self._active[name] = (downloaded, total)
            self._draw()

    def set_status(self, name: str, status: str, reason: str = ""):
        with self._lock:
            self._active.pop(name, None)
            short = ("…" + name[-40:]) if len(name) > 41 else name

            if status == "ok":
                self._done += 1
                sys.stdout.write(f"\r{' ' * 79}\r")
                print(f"  [✓] {short}")
            elif status == "fail":
                self._failed += 1
                self._errors.append((short, reason or "unknown"))
                sys.stdout.write(f"\r{' ' * 79}\r")
                print(f"  [✗] {short}  — {reason[:60]}")
            elif status == "bz2":
                self._active[name] = (-1, -1)

            self._draw()

    @property
    def errors(self) -> List[Tuple[str, str]]:
        return list(self._errors)

    def finalize(self):
        with self._lock:
            sys.stdout.write(f"\r{' ' * 79}\r")
            sys.stdout.flush()

    def _draw(self):
        pending = self._total - self._done - self._failed

        best_name, best_pct = "", -1
        extracting = 0
        for nm, (dl, total) in self._active.items():
            if dl == -1:
                extracting += 1
                continue
            if total > 0:
                pct = dl * 100 // total
                if pct > best_pct:
                    best_pct = pct
                    best_name = nm

        parts = [f"{self._done}✓", f"{self._failed}✗", f"{pending} left"]
        detail = ""
        if extracting:
            detail += f"  📦×{extracting}"
        if best_name and best_pct >= 0:
            sn = ("…" + best_name[-20:]) if len(best_name) > 21 else best_name
            detail += f"  ⬇ {sn} {best_pct}%"

        line = f"  [{' '.join(parts)}]{detail}"
        try:
            cols = os.get_terminal_size().columns
            if len(line) > cols - 1:
                line = line[:cols - 4] + "…"
        except Exception:
            pass

        sys.stdout.write(f"\r{line}{' ' * 10}\r{line}")
        sys.stdout.flush()


# ════════════════════════════════════════════════════════════════
#  SHARE CODE
# ════════════════════════════════════════════════════════════════

def decode_share_code(code: str) -> Tuple[int, int, int]:
    clean = code.replace("CSGO-", "").replace("-", "")
    if len(clean) != 25:
        raise ValueError(f"Expected 25 characters, got {len(clean)}")
    val = 0
    for ch in reversed(clean):
        idx = SHARECODE_ALPHABET.find(ch)
        if idx == -1: raise ValueError(f"Invalid character: {ch!r}")
        val = val * SHARECODE_BASE + idx
    raw = val.to_bytes(18, byteorder='big')
    return (
        int.from_bytes(raw[0:8],   "little"),
        int.from_bytes(raw[8:16],  "little"),
        int.from_bytes(raw[16:18], "little"),
    )

def csdm_name(mid, oid):
    return f"match730_{mid:021d}_{oid & 0xFFFFFFFF}"

def _selftest():
    m, r, t = decode_share_code("CSGO-GADqf-jjyJ8-cSP2r-smZRo-TO2xK")
    assert m == 3230642215713767580 and r == 3230647599455273103 and t == 55788


# ════════════════════════════════════════════════════════════════
#  DUPLICATE DETECTION + CLEANUP
# ════════════════════════════════════════════════════════════════

def _scan_folder(path: Path) -> _SafeSet:
    ids = set()
    for dem in path.rglob("*.dem"):   # rglob = subfolders too
        m = _RE_MID.search(dem.stem)
        if m:
            try:
                ids.add(str(int(m.group(1))))
            except (ValueError, TypeError):
                pass
    return _SafeSet(ids)

def _is_present(mid, known): return str(mid) in known

def _cleanup_tmp(dl_path: Path) -> int:
    count = 0
    for f in dl_path.glob("_tmp_*"):
        f.unlink(missing_ok=True); count += 1
    return count


# ════════════════════════════════════════════════════════════════
#  HTTP LAYER
# ════════════════════════════════════════════════════════════════

def _http_get_json(url, timeout=HTTP_TIMEOUT):
    try:
        if HAS_REQUESTS:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": "cs2dl/3"})
            r.raise_for_status(); return r.json()
        req = urllib.request.Request(url, headers={"User-Agent": "cs2dl/3"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        _safe_print(f"  [!] HTTP GET: {e}"); return None


def _http_download(url, dest, label="", timeout=DOWNLOAD_TIMEOUT,
                   on_progress=None) -> Tuple[bool, str]:
    """Returns (success, error_reason)."""
    last_error = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if HAS_REQUESTS:
                r = requests.get(url, stream=True, timeout=timeout,
                                 headers={"User-Agent": "cs2dl/3"})
                r.raise_for_status()
                total = int(r.headers.get("Content-Length", 0))
                dl = 0
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(CHUNK_SIZE):
                        f.write(chunk); dl += len(chunk)
                        if on_progress: on_progress(dl, total)
            else:
                req = urllib.request.Request(url, headers={"User-Agent": "cs2dl/3"})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    total = int(r.headers.get("Content-Length", 0))
                    dl = 0
                    with open(dest, "wb") as f:
                        while chunk := r.read(CHUNK_SIZE):
                            f.write(chunk); dl += len(chunk)
                            if on_progress: on_progress(dl, total)
            return True, ""
        except Exception as e:
            dest.unlink(missing_ok=True)
            last_error = str(e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * (2 ** (attempt - 1)))
    return False, last_error


# ════════════════════════════════════════════════════════════════
#  DOWNLOADED FILE VALIDATION
# ════════════════════════════════════════════════════════════════

def _validate_file(path: Path, is_bz2: bool) -> Optional[str]:
    if not path.exists():
        return "file missing"
    size = path.stat().st_size
    if size < 100:
        return f"too small ({size}B)"
    with open(path, "rb") as f:
        header = f.read(64)
    if b'<html' in header.lower() or header.startswith(b'<!'):
        return "HTML response (demo expired/unavailable)"
    if is_bz2 and not header.startswith(_BZ2_MAGIC):
        return f"not a valid bz2 (magic: {header[:4].hex()})"
    if not is_bz2 and not (header.startswith(b'PBDEMS2') or header.startswith(b'HL2DEMO')):
        return "not a valid .dem file"
    return None


# ════════════════════════════════════════════════════════════════
#  CONFIG
# ════════════════════════════════════════════════════════════════

def load_config():
    SCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if not isinstance(cfg, dict):
                return {"download_path": "", "players": []}
            cfg.setdefault("players", [])
            cfg.setdefault("download_path", "")
            return cfg
        except Exception:
            return {"download_path": "", "players": []}
    return {"download_path": "", "players": []}

def save_config(cfg):
    fd, tmp = tempfile.mkstemp(dir=CONFIG_FILE.parent, suffix=".tmp", prefix=".cfg_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        Path(tmp).replace(CONFIG_FILE)
    except Exception:
        Path(tmp).unlink(missing_ok=True); raise


# ════════════════════════════════════════════════════════════════
#  BOILER-WRITTER
# ════════════════════════════════════════════════════════════════

def _platform_info():
    s, m = platform.system(), platform.machine()
    a = {"AMD64":"AMD64","x86_64":"x86_64","ARM64":"ARM64",
         "aarch64":"aarch64","arm64":"arm64"}.get(m, m)
    return PLATFORM_ASSETS.get((s, a))

def _safe_zip_extract(zf, dest):
    dest = dest.resolve()
    for m in zf.namelist():
        if not (dest / m).resolve().is_relative_to(dest):
            raise ValueError(f"Path traversal: {m}")
    zf.extractall(dest)

def _safe_tar_extract(tf, dest):
    dest = dest.resolve()
    if sys.version_info >= (3, 12):
        tf.extractall(dest, filter='data')
    else:
        for m in tf.getmembers():
            if not (dest / m.name).resolve().is_relative_to(dest):
                raise ValueError(f"Path traversal: {m.name}")
        tf.extractall(dest)

def find_boiler():
    pi = _platform_info()
    if pi:
        d = BOILER_DIR / pi["bin"]
        if d.is_file(): return d
        if BOILER_DIR.exists():
            f = list(BOILER_DIR.rglob(pi["bin"]))
            if f: return f[0]
    e = shutil.which("boiler-writter") or shutil.which("boiler-writter.exe")
    return Path(e) if e else None

def install_boiler():
    pi = _platform_info()
    if not pi:
        print("  [✗] Unsupported platform"); return None
    print("  [→] GitHub → latest release…")
    rel = _http_get_json(GITHUB_BOILER_API)
    if not rel: return None
    assets = rel.get("assets", [])
    print(f"  [i] {rel.get('tag_name', '?')} — {len(assets)} assets")
    kw  = [k.lower() for k in pi["kw"]]
    exc = [k.lower() for k in pi["exc"]]
    ext = pi["ext"]
    asset = next(
        (a for a in assets
         if a["name"].lower().endswith(ext)
         and all(k in a["name"].lower() for k in kw)
         and not any(k in a["name"].lower() for k in exc)), None)
    if not asset:
        print("  [✗] Asset not found")
        for a in assets: print(f"      • {a['name']}")
        return None
    BOILER_DIR.mkdir(parents=True, exist_ok=True)
    arc = BOILER_DIR / asset["name"]
    ok, _ = _http_download(asset["browser_download_url"], arc, asset["name"])
    if not ok: return None
    digest = asset.get("digest", "")
    if digest.startswith("sha256:"):
        expected = digest.split(":", 1)[1]
        h = hashlib.sha256()
        with open(arc, "rb") as f:
            while chunk := f.read(CHUNK_SIZE):
                h.update(chunk)
        actual = h.hexdigest()
        if actual != expected:
            print(f"  [✗] Checksum mismatch! expected {expected[:12]}… got {actual[:12]}…")
            arc.unlink(missing_ok=True)
            return None
        print(f"  [✓] sha256 verified")
    else:
        print("  [!] No digest published for this asset — skipping integrity check")
    try:
        if arc.suffix == ".zip":
            with zipfile.ZipFile(arc) as z: _safe_zip_extract(z, BOILER_DIR)
        else:
            with tarfile.open(arc, "r:gz") as t: _safe_tar_extract(t, BOILER_DIR)
    except Exception as e:
        print(f"  [✗] {e}"); arc.unlink(missing_ok=True); return None
    finally:
        arc.unlink(missing_ok=True)
    for c in [BOILER_DIR / pi["bin"]] + list(BOILER_DIR.rglob(pi["bin"])):
        if c.is_file():
            if platform.system() != "Windows":
                c.chmod(c.stat().st_mode | 0o111)
            print(f"  [✓] {c}"); return c
    return None

def ensure_boiler():
    b = find_boiler()
    if b: return b
    print("\n[⚙]  Installing boiler-writter…")
    return install_boiler()


# ════════════════════════════════════════════════════════════════
#  BOILER CALLS
# ════════════════════════════════════════════════════════════════

def _kill_boiler(boiler):
    """Kill only OUR boiler-writter instance, matched by full path —
    not by bare process name, which could hit an unrelated process
    that happens to share the same name elsewhere on the machine."""
    full_path = str(boiler.resolve())
    try:
        if platform.system() == "Windows":
            # taskkill /IM only matches by image name (no path filter),
            # so query for the exact executable path first via wmic and
            # kill by PID; fall back to name-based taskkill if wmic fails.
            try:
                q = subprocess.run(
                    ["wmic", "process", "where",
                     f"ExecutablePath='{full_path}'", "get", "ProcessId"],
                    capture_output=True, timeout=5, text=True)
                pids = [p.strip() for p in q.stdout.splitlines()[1:] if p.strip().isdigit()]
                for pid in pids:
                    subprocess.run(["taskkill","/F","/PID",pid,"/T"],
                                   capture_output=True, timeout=5)
                if pids:
                    return
            except Exception:
                pass
            subprocess.run(["taskkill","/F","/IM",boiler.name,"/T"],
                           capture_output=True, timeout=5)
        else:
            subprocess.run(["pkill","-9","-f",full_path],
                           capture_output=True, timeout=5)
    except Exception:
        pass

def _boiler_url(boiler, mid, oid, token, *, _retry=False, debug=False):
    _kill_boiler(boiler)
    fd, out_str = tempfile.mkstemp(suffix=".bin")
    os.close(fd)
    out = Path(out_str)
    cmd = [str(boiler), str(out), str(mid), str(oid), str(token)]
    if debug: _safe_print(f"    [DEBUG] CMD: {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=BOILER_TIMEOUT)
        stderr = (r.stderr or b"").decode("utf-8", errors="replace").strip()
        if debug:
            _safe_print(f"    [DEBUG] exit={r.returncode}")
            if stderr: _safe_print(f"    [DEBUG] stderr: {stderr[:300]}")
        if r.returncode != 0:
            if r.returncode == 4 and "Already connected" in stderr and not _retry:
                _kill_boiler(boiler); time.sleep(6)
                return _boiler_url(boiler, mid, oid, token, _retry=True, debug=debug)
            _safe_print(f"    [!] {BOILER_EXIT_CODES.get(r.returncode, f'code {r.returncode}')}")
            return None, r.returncode
        if not out.exists() or out.stat().st_size == 0:
            return None, -1
        text = out.read_bytes().decode("latin-1", errors="replace")
        m = _RE_DEMO_URL.search(text)
        if m: return m.group(0), 0
        for u in _RE_FALLBACK_URL.findall(text):
            if any(w in u.lower() for w in ("dem","replay","valve")):
                return u, 0
        return None, -1
    except subprocess.TimeoutExpired:
        _safe_print("    [!] Boiler timeout"); _kill_boiler(boiler); return None, -1
    except Exception as e:
        _safe_print(f"    [!] {e}"); return None, -1
    finally:
        out.unlink(missing_ok=True)

def _boiler_recent_matches(boiler, debug=False):
    _kill_boiler(boiler)
    fd, out_str = tempfile.mkstemp(suffix=".bin")
    os.close(fd)
    out = Path(out_str)
    print("  [🔧] Testing GC connection…")
    try:
        r = subprocess.run([str(boiler), str(out)],
                           capture_output=True, timeout=BOILER_TIMEOUT)
        if r.returncode != 0:
            print(f"  [✗] {BOILER_EXIT_CODES.get(r.returncode, '?')}"); return False
        if out.exists():
            sz = out.stat().st_size
            print(f"  [✓] GC connected — {sz} bytes")
            if debug and sz:
                text = out.read_bytes().decode("latin-1", errors="replace")
                urls = _RE_DEMO_URL.findall(text)
                if urls:
                    print(f"  [DEBUG] {len(urls)} URL(s)")
                    for u in urls[:3]: print(f"           {u}")
            return True
        return False
    except Exception as e:
        print(f"  [✗] {e}"); return False
    finally:
        out.unlink(missing_ok=True)


# ════════════════════════════════════════════════════════════════
#  STEAM API — share code chaining
# ════════════════════════════════════════════════════════════════

def _next_code(api_key, steam_id, auth_code, known_code):
    """Returns (code, ok, http_status).
       ok=True  → API responded properly (code may be None = end of chain).
       ok=False → error; check http_status.
       202 → no new match yet  = clean end of chain.
       403 → wrong API key     = fatal, stop retrying.
       412 → share code mismatch = fatal, needs manual reset."""
    url = (f"{STEAM_NEXT_CODE_URL}?key={api_key}&steamid={steam_id}"
           f"&steamidkey={auth_code}&knowncode={known_code}")
    try:
        if HAS_REQUESTS:
            r = requests.get(url, timeout=10, headers={"User-Agent": "cs2dl/3"})
            if r.status_code == 200:
                c = r.json().get("result", {}).get("nextcode", "")
                if c and c != "n/a":
                    return c, True, 200
                return None, True, 200    # legitimate end of chain
            if r.status_code == 202:
                return None, True, 202    # no new match yet = clean end
            if r.status_code == 403:
                return None, False, 403   # wrong API key — stop retrying
            if r.status_code == 412:
                return None, False, 412   # share code mismatch — needs reset
            return None, False, r.status_code
        else:
            req = urllib.request.Request(url, headers={"User-Agent": "cs2dl/3"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
            c = data.get("result", {}).get("nextcode", "")
            if c and c != "n/a":
                return c, True, 200
            return None, True, 200
    except Exception:
        return None, False, -1

def collect_codes(player, known_ids):
    start = player.get("last_known_code") or player.get("oldest_share_code", "")
    if not start:
        print("  [!] No share code configured."); return []
    codes = []
    try:
        mid, _, _ = decode_share_code(start)
        if not _is_present(mid, known_ids): codes.append(start)
    except ValueError:
        pass
    cur, seen = start, {start}
    failures = 0
    max_failures = 5
    while True:
        nxt, ok, status = _next_code(player["api_key"], player["steam_id"],
                                     player["auth_code"], cur)
        if not ok:
            if status == 403:
                print("  [✗]  API returned 403 — check your API key and auth code")
                break
            if status == 412:
                print("  [✗]  API returned 412 — share code mismatch, "
                      "use option 8 and enter a recent code from this account")
                break
            failures += 1
            if failures >= max_failures:
                print(f"  [⚠]  API failed {failures} times in a row, stopping chain")
                break
            print(f"  [⚠]  API call failed (HTTP {status}), "
                  f"retrying ({failures}/{max_failures})…")
            time.sleep(RETRY_BACKOFF * failures)
            continue
        failures = 0                # reset on success
        if not nxt or nxt in seen:
            break                   # legitimate end of chain
        seen.add(nxt); codes.append(nxt); cur = nxt
        time.sleep(API_RATE_DELAY)
    print(f"  [→] {len(codes)} new match(es)")
    return codes


# ════════════════════════════════════════════════════════════════
#  PHASE 1 — resolve URLs via boiler
# ════════════════════════════════════════════════════════════════

def resolve_urls(codes, boiler, known_ids, dl_path, debug=False):
    tasks, total = [], len(codes)
    stats = {"resolved":0, "expired":0, "skipped":0, "errors":0, "last_ok_code":None}
    for i, code in enumerate(codes, 1):
        pre = f"  [{i}/{total}]"
        try:
            mid, oid, token = decode_share_code(code)
        except ValueError as e:
            print(f"{pre} [⏭]  Invalid: {code[:40]}… ({e})")
            stats["errors"] += 1; continue
        if debug:
            print(f"{pre} [DEBUG] mid={mid} oid={oid} tok={token}")
        if _is_present(mid, known_ids):
            print(f"{pre} [⏭]  Already present")
            stats["skipped"] += 1; stats["last_ok_code"] = code; continue
        name  = csdm_name(mid, oid)
        final = dl_path / f"{name}.dem"
        if final.exists():
            known_ids.add(str(mid))
            print(f"{pre} [⏭]  File exists")
            stats["skipped"] += 1; stats["last_ok_code"] = code; continue
        print(f"{pre} [🔍]  {code}  →  boiler…")
        url, rc = _boiler_url(boiler, mid, oid, token, debug=debug)
        if not url:
            if rc == 8:
                stats["expired"] += 1
                print(f"{pre} [⌛]  Expired")
                stats["last_ok_code"] = code   # permanently gone, safe to skip
            else:
                stats["errors"] += 1
                print(f"{pre} [!]   No URL (boiler rc={rc})")
                # do NOT advance cursor — boiler errors are transient, retry next run
        else:
            tasks.append({"mid":mid, "oid":oid, "name":name, "url":url, "code":code})
            stats["resolved"] += 1
            print(f"{pre} [✓]   URL ready")
            # cursor advances only after confirmed download (handled in run_scan)
        if i < total: time.sleep(BOILER_DELAY)
    return tasks, stats


# ════════════════════════════════════════════════════════════════
#  PHASE 2 — parallel downloads
# ════════════════════════════════════════════════════════════════

def _decompress_bz2(src, dest):
    try:
        d = bz2.BZ2Decompressor()
        with open(src, "rb") as fi, open(dest, "wb") as fo:
            while chunk := fi.read(CHUNK_SIZE):
                fo.write(d.decompress(chunk))
        return True, ""
    except Exception as e:
        return False, str(e)


def _dl_one(task, dl_path, known_ids, display=None):
    mid, oid = task["mid"], task["oid"]
    name, url = task["name"], task["url"]
    final = dl_path / f"{name}.dem"

    # Check exact file AND any file with same match id, recursively,
    # including non-zero-padded names from other tools
    mid_str   = f"match730_{mid:021d}_"
    mid_plain = str(mid)
    already = (
        final.exists()
        or _is_present(mid, known_ids)
        or any(dl_path.rglob(f"{mid_str}*.dem"))
        or any(dl_path.rglob(f"*{mid_plain}*.dem"))
    )
    if already:
        known_ids.add(str(mid))
        if display:
            display.assign(name)
            display.set_status(name, "ok")
        return True, ""

    if display: display.assign(name)

    is_bz2 = url.endswith(".bz2")
    suffix = ".dem.bz2" if is_bz2 else ".dem"
    tmp = dl_path / f"_tmp_{name}{suffix}"

    def on_progress(dl, total):
        if display: display.update(name, dl, total)

    ok, err = _http_download(url, tmp, name[-22:], on_progress=on_progress)
    if not ok:
        tmp.unlink(missing_ok=True)
        if any(code in err for code in ("502", "503", "504")):
            reason = f"Valve CDN error (retry next run): {err[:60]}"
        else:
            reason = f"Download failed: {err[:80]}"
        if display: display.set_status(name, "fail", reason)
        return False, reason

    verr = _validate_file(tmp, is_bz2)
    if verr:
        tmp.unlink(missing_ok=True)
        if display: display.set_status(name, "fail", verr)
        return False, verr

    if is_bz2:
        if display: display.set_status(name, "bz2")
        dem = dl_path / f"_tmp_{name}.dem"
        ok, berr = _decompress_bz2(tmp, dem)
        tmp.unlink(missing_ok=True)
        if not ok:
            dem.unlink(missing_ok=True)
            reason = f"bz2: {berr[:80]}"
            if display: display.set_status(name, "fail", reason)
            return False, reason
        dem.rename(final)
    else:
        tmp.rename(final)

    if display: display.set_status(name, "ok")
    known_ids.add(str(mid))
    return True, ""


def download_all(tasks, dl_path, known_ids):
    if not tasks: return []
    n = len(tasks)
    workers = min(DOWNLOAD_WORKERS, n)
    print(f"\n[⬇]  {n} demo(s) — {workers} workers\n")

    display = _ProgressDisplay(total_count=n)
    succeeded = []

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_dl_one, t, dl_path, known_ids, display): t for t in tasks}
        for f in as_completed(futs):
            task = futs[f]
            try:
                ok, reason = f.result()
                if ok:
                    succeeded.append(task)
            except Exception as e:
                display.set_status(task["name"], "fail", str(e))

    display.finalize()
    print()

    errors = display.errors
    if errors:
        reasons: Dict[str, List[str]] = {}
        for nm, reason in errors:
            reasons.setdefault(reason, []).append(nm)
        print(f"  [📋] {len(errors)} failure(s):")
        for reason, names in reasons.items():
            print(f"\n    ▸ {reason}  ({len(names)})")
            for nm in names[:5]:
                print(f"      — {nm}")
            if len(names) > 5:
                print(f"      … +{len(names)-5}")
        print()

    return succeeded


# ════════════════════════════════════════════════════════════════
#  MAIN SCAN
# ════════════════════════════════════════════════════════════════

def run_scan(cfg, dl_path, debug=False):
    players = cfg.get("players", [])
    if not players:
        print("\n  [!] No players — add one (option 2)."); return
    boiler = ensure_boiler()
    if not boiler:
        print("\n  [✗] boiler-writter unavailable."); return
    print(f"\n[⚙]  Boiler: {boiler}")
    print("[⚠]  Steam must be running and logged in. CS2 must be CLOSED.\n")
    if debug:
        _boiler_recent_matches(boiler, debug=True); print()

    known_ids = _scan_folder(dl_path)
    cleaned = _cleanup_tmp(dl_path)
    if cleaned:
        print(f"[🧹]  {cleaned} orphaned temp file(s) removed")
    print(f"[📂]  {len(known_ids)} demo(s) already indexed.\n")

    all_tasks, task_player, player_cursor = [], {}, {}
    seen_mids: set = set()

    for player in players:
        pid = player["steam_id"]
        print(f"{'═'*62}\n  {player['name']}  ({pid})\n{'═'*62}")
        codes = collect_codes(player, known_ids)
        if not codes:
            print("  [i] Nothing new.\n"); continue
        tasks, stats = resolve_urls(codes, boiler, known_ids, dl_path, debug=debug)

        unique_tasks = []
        dupes = 0
        for t in tasks:
            mid_str = str(t["mid"])
            if mid_str in seen_mids:
                dupes += 1; continue
            seen_mids.add(mid_str)
            known_ids.add(mid_str)
            unique_tasks.append(t)

        parts = []
        if unique_tasks:      parts.append(f"{len(unique_tasks)} to DL")
        if dupes:             parts.append(f"{dupes} already queued")
        if stats["expired"]:  parts.append(f"{stats['expired']} expired")
        if stats["skipped"]:  parts.append(f"{stats['skipped']} already present")
        if stats["errors"]:   parts.append(f"{stats['errors']} error(s)")
        print(f"  [Σ] {' | '.join(parts) if parts else 'nothing'}")
        if stats["expired"] and not stats["resolved"]:
            print(f"\n  [⚠]  All expired → option 3 or 8.\n")
        if stats["last_ok_code"]:
            player_cursor[pid] = stats["last_ok_code"]
        for t in unique_tasks:
            task_player[t["name"]] = player
        all_tasks.extend(unique_tasks)
        print()

    for p in players:
        if p["steam_id"] in player_cursor:
            p["last_known_code"] = player_cursor[p["steam_id"]]

    succeeded = download_all(all_tasks, dl_path, known_ids)

    order = {t["name"]: i for i, t in enumerate(all_tasks)}
    best: Dict[str, Tuple[int, str]] = {}
    for t in succeeded:
        p = task_player.get(t["name"])
        if not p: continue
        pid = p["steam_id"]
        o   = order.get(t["name"], -1)
        c   = t.get("code", "")
        if c:
            prev = best.get(pid, (-1, ""))
            if o > prev[0]: best[pid] = (o, c)
    for p in players:
        if p["steam_id"] in best:
            p["last_known_code"] = best[p["steam_id"]][1]

    save_config(cfg)
    print(f"{'═'*62}")
    print(f"  Done — {len(succeeded)}/{len(all_tasks)} demo(s) downloaded")
    print(f"  {dl_path}")
    print(f"{'═'*62}")


# ════════════════════════════════════════════════════════════════
#  UI
# ════════════════════════════════════════════════════════════════

def _vs(sid): return bool(_RE_STEAMID.match(sid))
def _vc(code):
    if not code: return False
    try: decode_share_code(code); return True
    except ValueError: return False

def ask(p, d=""):
    v = input(f"{p}{f' [{d}]' if d else ''}: ").strip()
    return v or d

def clean_path(r):
    r = r.strip()
    for q in ('"', "'"):
        if r.startswith(q) and r.endswith(q) and len(r) > 1:
            r = r[1:-1].strip()
    return r

def ask_dl_path(cfg):
    cur = cfg.get("download_path", "")
    if cur:
        p = Path(cur)
        if p.is_dir(): return p
        print(f"\n[!]  Folder not found: {cur}")
    while True:
        raw = clean_path(ask("\n[📁]  Download folder",
                             str(Path.home() / "CS2_Demos")))
        path = Path(raw).expanduser().resolve()
        try:
            path.mkdir(parents=True, exist_ok=True)
            cfg["download_path"] = str(path); save_config(cfg)
            print(f"       → {path}"); return path
        except Exception as e:
            print(f"  [✗] {e}")

def change_dl_path(cfg):
    cfg["download_path"] = ""; return ask_dl_path(cfg)

def add_player(cfg):
    print("\n" + "─" * 62)
    print("  API Key  : https://steamcommunity.com/dev/apikey")
    print("  Auth code: https://help.steampowered.com/en/wizard/")
    print("             HelpWithGameIssue/?appid=730&issueid=128")
    print("  SteamID  : https://steamid.io/")
    print("─" * 62)
    name = ask("\n  Nickname")
    sid  = ask("  SteamID64")
    if not _vs(sid): print("  [!] Invalid SteamID64."); return
    key  = ask("  Steam API Key")
    if not key: print("  [!] Required."); return
    auth = ask("  Auth code")
    if not auth: print("  [!] Required."); return
    code = ask("  Starting share code")
    if not _vc(code): print("  [!] Invalid."); return
    cfg.setdefault("players", []).append({
        "name": name or sid, "steam_id": sid,
        "api_key": key, "auth_code": auth,
        "oldest_share_code": code, "last_known_code": code,
    })
    save_config(cfg)
    print(f"  [✓] {name or sid} added.")

def list_players(cfg):
    ps = cfg.get("players", [])
    if not ps: print("\n  No players."); return
    for i, p in enumerate(ps, 1):
        print(f"\n  {i}. {p['name']} — {p['steam_id']}")
        print(f"       Last code: {p.get('last_known_code', '—')}")
        o = p.get('oldest_share_code', '—')
        if o != p.get('last_known_code', ''):
            print(f"       Initial  : {o}")

def edit_player(cfg):
    ps = cfg.get("players", [])
    if not ps: print("\n  No players."); return
    list_players(cfg)
    try: p = ps[int(ask("\n  Player number")) - 1]
    except (ValueError, IndexError): print("  [!] Invalid."); return
    print(f"\n  {p['name']} (Enter = keep current)")
    for f, l, v in [
        ("name",            "Nickname",    None),
        ("steam_id",        "SteamID64",   _vs),
        ("api_key",         "API Key",     None),
        ("auth_code",       "Auth code",   None),
        ("last_known_code", "Share code",  _vc),
    ]:
        val = ask(f"  {l}", p.get(f, ""))
        if val and val != p.get(f, ""):
            if v and not v(val): print(f"    [!] Invalid, kept old value.")
            else: p[f] = val
    save_config(cfg)
    print("  [✓] Updated.")

def remove_player(cfg):
    ps = cfg.get("players", [])
    if not ps: print("\n  No players."); return
    list_players(cfg)
    try:
        p = ps.pop(int(ask("\n  Player number")) - 1)
        save_config(cfg)
        print(f"  [✓] {p['name']} removed.")
    except (ValueError, IndexError):
        print("  [!] Invalid.")

def reset_player_code(cfg):
    ps = cfg.get("players", [])
    if not ps: print("\n  No players."); return
    list_players(cfg)
    try: p = ps[int(ask("\n  Player number")) - 1]
    except (ValueError, IndexError): print("  [!] Invalid."); return
    print(f"\n  {p['name']}")
    print(f"  Current: {p.get('last_known_code', '—')}")
    print(f"  Initial: {p.get('oldest_share_code', '—')}")
    print("\n  1. Revert to initial code\n  2. Enter a new code")
    c = ask("  Choice", "2")
    if c == "1":
        o = p.get("oldest_share_code", "")
        if o:
            p["last_known_code"] = o; save_config(cfg)
            print(f"  [✓] → {o}")
        else:
            print("  [!] No initial code.")
    elif c == "2":
        code = ask("  New share code")
        if _vc(code):
            p["last_known_code"] = code
            p["oldest_share_code"] = code
            save_config(cfg)
            print(f"  [✓] → {code}")
        else:
            print("  [!] Invalid.")

def test_gc(cfg):
    b = ensure_boiler()
    if not b: return
    print(f"\n[⚙]  {b}")
    _boiler_recent_matches(b, debug=True)


# ════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════

def main():
    print("\n╔══════════════════════════════════════════════════════════╗")
    print("║            CS2 Demo Downloader — Leetify Style           ║")
    print("╚══════════════════════════════════════════════════════════╝")
    if not HAS_REQUESTS:
        print("\n[⚠]  pip install requests\n")

    assert len(SHARECODE_ALPHABET) == 57
    _selftest()
    print("[✓]  Self-test OK")

    cfg     = load_config()
    dl_path = ask_dl_path(cfg)
    debug   = False

    print(f"[📁]  Folder: {dl_path}")

    while True:
        print("\n" + "─" * 62)
        print("  1. Scan and download")
        print("  2. Add a player")
        print("  3. Edit a player")
        print("  4. Remove a player")
        print("  5. List players")
        print("  6. Change download folder")
        print("  7. Reinstall boiler-writter")
        print("  8. Reset share code")
        print("  9. Test GC connection")
        print(f"  D. Debug: {'ON 🟢' if debug else 'OFF ⚪'}")
        print("  0. Quit")
        print("─" * 62)
        c = ask("  Choice", "1").upper()

        if   c == "1": run_scan(cfg, dl_path, debug=debug)
        elif c == "2": add_player(cfg)
        elif c == "3": edit_player(cfg)
        elif c == "4": remove_player(cfg)
        elif c == "5": list_players(cfg)
        elif c == "6":
            dl_path = change_dl_path(cfg)
            print(f"[📁]  {dl_path}")
        elif c == "7":
            if BOILER_DIR.exists(): shutil.rmtree(BOILER_DIR)
            install_boiler()
        elif c == "8": reset_player_code(cfg)
        elif c == "9": test_gc(cfg)
        elif c == "D":
            debug = not debug
            print(f"  Debug {'ON 🟢' if debug else 'OFF ⚪'}")
        elif c == "0":
            print("\n  Goodbye!\n"); break
        else:
            print("  [!] ?")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Interrupted.\n")
        sys.exit(0)