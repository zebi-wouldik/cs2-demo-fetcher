#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CS2 Demo Downloader — GUI Edition
Downloads demos via Steam API (auth code, like Leetify).
CSDM naming: match730_MATCHID_RESERVATIONID.dem
"""

__version__ = "3.3.3"

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
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, List, Dict

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ════════════════════════════════════════════════════════════════
#  PATHS
# ════════════════════════════════════════════════════════════════

SCRIPT_DIR  = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
BOILER_DIR  = SCRIPT_DIR / "boiler"

# ════════════════════════════════════════════════════════════════
#  THEME
# ════════════════════════════════════════════════════════════════

PAL = {
    "bg": "#1e1e1e", "fg": "#d4d4d4", "alt_bg": "#252526",
    "sel_bg": "#094771", "sel_fg": "#ffffff",
    "btn_bg": "#333333", "btn_hov": "#404040",
    "link": "#4a90d9", "accent": "#007acc",
    "ok": "#4ec9b0", "fail": "#f44747", "warn": "#dcdcaa", "info": "#569cd6",
}

def _apply_theme(root):
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")

    style.configure(".", background=PAL["bg"], foreground=PAL["fg"],
                    fieldbackground=PAL["alt_bg"], insertcolor=PAL["fg"],
                    troughcolor=PAL["bg"],
                    selectbackground=PAL["sel_bg"], selectforeground=PAL["sel_fg"])
    style.configure("TButton", background=PAL["btn_bg"], borderwidth=0,
                    focusthickness=1, focuscolor=PAL["sel_bg"])
    style.map("TButton", background=[("active", PAL["btn_hov"])])
    style.configure("TLabel", background=PAL["bg"])
    style.configure("TLabelframe", background=PAL["bg"], bordercolor=PAL["btn_bg"])
    style.configure("TLabelframe.Label", background=PAL["bg"])
    style.configure("TScrollbar", background=PAL["btn_bg"],
                    troughcolor=PAL["bg"], arrowcolor=PAL["fg"], borderwidth=0)
    style.configure("Horizontal.TProgressbar",
                    troughcolor=PAL["alt_bg"], background=PAL["accent"])
    style.configure("Action.TButton", padding=(10, 6))
    root.configure(bg=PAL["bg"])


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

PLATFORM_ASSETS = {
    ("Windows","AMD64"):  {"kw":["win"],         "exc":["mac","linux"],"bin":"boiler-writter.exe","ext":".zip"},
    ("Windows","ARM64"):  {"kw":["win"],         "exc":["mac","linux"],"bin":"boiler-writter.exe","ext":".zip"},
    ("Linux","x86_64"):   {"kw":["linux"],       "exc":["mac","win"],  "bin":"boiler-writter",    "ext":".zip"},
    ("Linux","aarch64"):  {"kw":["linux"],       "exc":["mac","win"],  "bin":"boiler-writter",    "ext":".zip"},
    ("Darwin","x86_64"):  {"kw":["mac"],         "exc":["arm64","win"],"bin":"boiler-writter",    "ext":".zip"},
    ("Darwin","arm64"):   {"kw":["mac","arm64"], "exc":["win"],        "bin":"boiler-writter",    "ext":".zip"},
}

_RE_STEAMID      = re.compile(r'^\d{17}$')
_RE_BIGNUM       = re.compile(r'\d{8,21}')
_RE_DEMO_URL     = re.compile(r'https?://[^\x00-\x1f\x7f\s]{15,}\.dem(?:\.bz2)?')
_RE_FALLBACK_URL = re.compile(r'https?://[^\x00-\x1f\x7f\s]{15,}')
_BZ2_MAGIC       = b'BZ'

# ◄◄◄ FIXED — sentinel to distinguish "skipped" from "downloaded"
_SKIPPED = "__skipped__"

# ════════════════════════════════════════════════════════════════
#  THREAD-SAFE UTILS
# ════════════════════════════════════════════════════════════════

class _SafeSet:
    __slots__ = ('_data', '_lock')

    def __init__(self, initial=None):
        self._data = set(initial) if initial else set()
        self._lock = threading.Lock()

    def add(self, item):
        with self._lock:
            self._data.add(item)

    def update(self, items):
        with self._lock:
            self._data.update(items)

    def __contains__(self, item):
        with self._lock:
            return item in self._data

    def __len__(self):
        with self._lock: return len(self._data)

# ════════════════════════════════════════════════════════════════
#  PATH HELPERS (Python 3.8 compatible)
# ════════════════════════════════════════════════════════════════

def _is_safe_path(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


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

def csdm_name(mid, oid, token):
    return f"match730_{mid:021d}_{oid & 0xFFFFFFFF:010d}_{token}"

def _selftest():
    m, r, t = decode_share_code("CSGO-GADqf-jjyJ8-cSP2r-smZRo-TO2xK")
    assert m == 3230642215713767580 and r == 3230647599455273103 and t == 55788

def _valid_share_code(code):
    try: decode_share_code(code); return True
    except ValueError: return False


# ════════════════════════════════════════════════════════════════
#  DUPLICATE DETECTION + CLEANUP
# ════════════════════════════════════════════════════════════════

def _scan_folder(path: Path) -> _SafeSet:
    """Index every ≥8-digit number found in every .dem filename — not just
    the first one, and not assuming which position holds matchId vs
    reservationId. Works regardless of which tool wrote the file."""
    ids = set()
    # rglob instead of glob → scans subfolders too
    for dem in path.rglob("*.dem"):
        for m in _RE_BIGNUM.finditer(dem.stem):
            try:
                ids.add(str(int(m.group(0))))   # normalize away leading zeros
            except ValueError:
                pass
    return _SafeSet(ids)

def _match_keys(mid, oid=None):
    keys = {str(mid)}
    if oid is not None:
        keys.add(str(oid))
        keys.add(str(oid & 0xFFFFFFFF))
    return keys

def _is_present(known, mid, oid=None):
    return any(k in known for k in _match_keys(mid, oid))

def _register(known, mid, oid=None):
    known.update(_match_keys(mid, oid))

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
            r = requests.get(url, timeout=timeout, headers={"User-Agent": "cs2dl/4"})
            r.raise_for_status(); return r.json()
        req = urllib.request.Request(url, headers={"User-Agent": "cs2dl/4"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None

def _http_download(url, dest, timeout=DOWNLOAD_TIMEOUT, on_progress=None):
    last_error = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if HAS_REQUESTS:
                r = requests.get(url, stream=True, timeout=timeout,
                                 headers={"User-Agent": "cs2dl/4"})
                r.raise_for_status()
                total = int(r.headers.get("Content-Length", 0))
                dl = 0
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(CHUNK_SIZE):
                        f.write(chunk); dl += len(chunk)
                        if on_progress: on_progress(dl, total)
            else:
                req = urllib.request.Request(url, headers={"User-Agent": "cs2dl/4"})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    total = int(r.headers.get("Content-Length", 0))
                    dl = 0
                    with open(dest, "wb") as f:
                        while True:
                            chunk = r.read(CHUNK_SIZE)
                            if not chunk: break
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
#  FILE VALIDATION
# ════════════════════════════════════════════════════════════════

def _validate_file(path: Path, is_bz2: bool):
    if not path.exists(): return "file missing"
    if path.stat().st_size < 100: return "file too small"
    with open(path, "rb") as f: header = f.read(64)
    if b'<html' in header.lower() or header.startswith(b'<!'):
        return "HTML response (demo expired/unavailable)"
    if is_bz2 and not header.startswith(_BZ2_MAGIC):
        return "not a valid bz2"
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
            if isinstance(cfg, dict):
                cfg.setdefault("players", [])
                cfg.setdefault("download_path", "")
                return cfg
        except Exception:
            pass
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
    for name in zf.namelist():
        if not _is_safe_path(dest, dest / name):
            raise ValueError(f"Path traversal: {name}")
    zf.extractall(dest)

def _safe_tar_extract(tf, dest):
    dest = dest.resolve()
    if sys.version_info >= (3, 12):
        tf.extractall(dest, filter='data')
    else:
        for m in tf.getmembers():
            if not _is_safe_path(dest, dest / m.name):
                raise ValueError(f"Path traversal: {m.name}")
        tf.extractall(dest)

def find_boiler():
    pi = _platform_info()
    if pi:
        d = BOILER_DIR / pi["bin"]
        if d.is_file(): return d
        if BOILER_DIR.exists():
            found = list(BOILER_DIR.rglob(pi["bin"]))
            if found: return found[0]
    e = shutil.which("boiler-writter") or shutil.which("boiler-writter.exe")
    return Path(e) if e else None

def install_boiler(log=print):
    pi = _platform_info()
    if not pi: log("  [✗] Unsupported platform"); return None
    log("  [→] GitHub → latest release…")
    rel = _http_get_json(GITHUB_BOILER_API)
    if not rel: log("  [✗] Failed to reach GitHub"); return None
    assets = rel.get("assets", [])
    log(f"  [i] {rel.get('tag_name', '?')} — {len(assets)} assets")
    kw  = [k.lower() for k in pi["kw"]]
    exc = [k.lower() for k in pi["exc"]]
    asset = next(
        (a for a in assets if a["name"].lower().endswith(pi["ext"])
         and all(k in a["name"].lower() for k in kw)
         and not any(k in a["name"].lower() for k in exc)), None)
    if not asset: log("  [✗] Matching asset not found"); return None
    BOILER_DIR.mkdir(parents=True, exist_ok=True)
    arc = BOILER_DIR / asset["name"]
    log(f"  [→] Downloading {asset['name']}…")
    ok, err = _http_download(asset["browser_download_url"], arc)
    if not ok: log(f"  [✗] Download failed: {err}"); return None
    digest = asset.get("digest", "")
    if digest.startswith("sha256:"):
        expected = digest.split(":", 1)[1]
        h = hashlib.sha256()
        with open(arc, "rb") as f:
            while chunk := f.read(CHUNK_SIZE):
                h.update(chunk)
        if h.hexdigest() != expected:
            log(f"  [✗] Checksum mismatch — aborting")
            arc.unlink(missing_ok=True); return None
        log(f"  [✓] sha256 verified")
    else:
        log("  [!] No digest published — skipping integrity check")
    try:
        if arc.suffix == ".zip":
            with zipfile.ZipFile(arc) as z: _safe_zip_extract(z, BOILER_DIR)
        else:
            with tarfile.open(arc, "r:gz") as t: _safe_tar_extract(t, BOILER_DIR)
    except Exception as e:
        log(f"  [✗] Extraction: {e}"); arc.unlink(missing_ok=True); return None
    finally:
        arc.unlink(missing_ok=True)
    for c in [BOILER_DIR / pi["bin"]] + list(BOILER_DIR.rglob(pi["bin"])):
        if c.is_file():
            if platform.system() != "Windows": c.chmod(c.stat().st_mode | 0o111)
            log(f"  [✓] Installed: {c}"); return c
    log("  [✗] Binary not found after extraction"); return None

def ensure_boiler(log=print):
    b = find_boiler()
    if b: return b
    log("[⚙]  Installing boiler-writter…")
    return install_boiler(log)


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
    except Exception: pass

def _boiler_url(boiler, mid, oid, token, *, _retry=False, log=print):
    _kill_boiler(boiler)
    fd, out_str = tempfile.mkstemp(suffix=".bin")
    os.close(fd)
    out = Path(out_str)
    try:
        r = subprocess.run(
            [str(boiler), str(out), str(mid), str(oid), str(token)],
            capture_output=True, timeout=BOILER_TIMEOUT)
        stderr = (r.stderr or b"").decode("utf-8", errors="replace").strip()
        if r.returncode != 0:
            if r.returncode == 4 and "Already connected" in stderr and not _retry:
                _kill_boiler(boiler); time.sleep(6)
                return _boiler_url(boiler, mid, oid, token, _retry=True, log=log)
            msg = BOILER_EXIT_CODES.get(r.returncode, f"code {r.returncode}")
            log(f"    [!] {msg}")
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
        log("    [!] Boiler timeout"); _kill_boiler(boiler); return None, -1
    except Exception as e:
        log(f"    [!] {e}"); return None, -1
    finally:
        out.unlink(missing_ok=True)

def _boiler_test(boiler, log=print):
    _kill_boiler(boiler)
    fd, out_str = tempfile.mkstemp(suffix=".bin")
    os.close(fd)
    out = Path(out_str)
    log("  [🔧] Testing GC connection…")
    try:
        r = subprocess.run([str(boiler), str(out)],
                           capture_output=True, timeout=BOILER_TIMEOUT)
        if r.returncode != 0:
            msg = BOILER_EXIT_CODES.get(r.returncode, '?')
            log(f"  [✗] GC: {msg}"); return False
        if out.exists() and out.stat().st_size > 0:
            log(f"  [✓] GC connected — {out.stat().st_size} bytes"); return True
        log("  [✗] Empty response"); return False
    except Exception as e:
        log(f"  [✗] {e}"); return False
    finally:
        out.unlink(missing_ok=True)


# ════════════════════════════════════════════════════════════════
#  STEAM API — share code chaining
# ════════════════════════════════════════════════════════════════

def _next_code(api_key, steam_id, auth_code, known_code, log=print):
    """Returns (code, ok, http_status)
       ok=True  → API responded properly (code may still be None = end of chain).
       ok=False → transient or fatal error; check http_status.
       http_status=403 → wrong API key (fatal).
       http_status=412 → share code mismatch (fatal, needs reset).
       http_status=202 → no new match yet = legitimate end of chain."""
    url = (f"{STEAM_NEXT_CODE_URL}?key={api_key}&steamid={steam_id}"
           f"&steamidkey={auth_code}&knowncode={known_code}")
    try:
        if HAS_REQUESTS:
            r = requests.get(url, timeout=10, headers={"User-Agent": "cs2dl/4"})
            if r.status_code == 200:
                c = r.json().get("result", {}).get("nextcode", "")
                if c and c != "n/a":
                    return c, True, 200
                return None, True, 200    # legitimate end of chain
            if r.status_code == 202:
                return None, True, 202    # "no new match yet" = legitimate end, not an error
            if r.status_code == 403:
                return None, False, 403   # wrong API key — no point retrying
            if r.status_code == 412:
                return None, False, 412   # share code mismatch — needs manual reset
            return None, False, r.status_code
        else:
            req = urllib.request.Request(url, headers={"User-Agent": "cs2dl/4"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
            c = data.get("result", {}).get("nextcode", "")
            if c and c != "n/a":
                return c, True, 200
            return None, True, 200
    except Exception:
        return None, False, -1

def collect_codes(player, known_ids, log=print):
    start = player.get("last_known_code") or player.get("oldest_share_code", "")
    if not start: log("  [!] No share code configured."); return []
    codes = []
    try:
        mid, oid, _ = decode_share_code(start)
        if not _is_present(known_ids, mid, oid): codes.append(start)
    except ValueError: pass
    cur, seen = start, {start}
    failures = 0
    max_failures = 5
    while True:
        nxt, ok, status = _next_code(player["api_key"], player["steam_id"],
                                     player["auth_code"], cur, log)
        if not ok:
            # 403 = wrong API key or auth code — no point retrying
            if status == 403:
                log("  [✗]  API returned 403 — check your API key and auth code")
                break
            # 412 = share code doesn't match this account — needs manual reset
            if status == 412:
                log("  [✗]  API returned 412 — share code mismatch, "
                    "use 'Reset Share Code' and enter a recent one from this account")
                break
            failures += 1
            if failures >= max_failures:
                log(f"  [⚠]  API failed {failures} times in a row, stopping chain")
                break
            log(f"  [⚠]  API call failed (HTTP {status}), retrying "
                f"({failures}/{max_failures})…")
            time.sleep(RETRY_BACKOFF * failures)
            continue
        failures = 0                # reset on success
        if not nxt or nxt in seen:
            break                   # legitimate end of chain
        seen.add(nxt); codes.append(nxt); cur = nxt
        time.sleep(API_RATE_DELAY)
    log(f"  [→] {len(codes)} new match(es)")
    return codes

# ════════════════════════════════════════════════════════════════
#  PHASE 1 — resolve URLs via boiler
# ════════════════════════════════════════════════════════════════

def resolve_urls(codes, boiler, known_ids, dl_path, log=print):
    tasks, total = [], len(codes)
    # timeline = chain-ordered (code, state) used to advance the cursor safely.
    #   state True  → definitively done, cursor may pass
    #   state False → transient failure, cursor must STOP here
    #   state int   → match id, resolved after the download phase
    stats = {"resolved":0, "expired":0, "skipped":0, "errors":0,
             "last_ok_code":None, "timeline":[]}
    for i, code in enumerate(codes, 1):
        pre = f"  [{i}/{total}]"
        try:
            mid, oid, token = decode_share_code(code)
        except ValueError:
            log(f"{pre} [⏭]  Invalid: {code[:40]}…")
            stats["errors"] += 1
            stats["timeline"].append((code, True))   # never decodable, skip forever
            continue

        if _is_present(known_ids, mid, oid):
            stats["skipped"] += 1; stats["last_ok_code"] = code
            stats["timeline"].append((code, True)); continue

        name  = csdm_name(mid, oid, token)
        final = dl_path / f"{name}.dem"

        # also glob for any file with this match id (different oid/token)
        mid_str = f"match730_{mid:021d}_"
        already_on_disk = final.exists() or any(dl_path.glob(f"{mid_str}*.dem"))

        if already_on_disk:
            _register(known_ids, mid, oid)
            stats["skipped"] += 1; stats["last_ok_code"] = code
            stats["timeline"].append((code, True)); continue

        log(f"{pre} [🔍]  {code}  →  boiler…")
        url, rc = _boiler_url(boiler, mid, oid, token, log=log)
        if not url:
            if rc == 8:
                stats["expired"] += 1; log(f"{pre} [⌛]  Expired")
                stats["last_ok_code"] = code   # expired = permanently gone, safe to skip
                stats["timeline"].append((code, True))
            else:
                stats["errors"] += 1; log(f"{pre} [!]   No URL (boiler rc={rc})")
                # boiler errors are transient → cursor must stop here so the
                # match is re-enumerated next run
                stats["timeline"].append((code, False))
        else:
            tasks.append({"mid":mid, "oid":oid, "name":name, "url":url, "code":code})
            stats["resolved"] += 1; log(f"{pre} [✓]   URL ready")
            # cursor advances only if this one really lands on disk
            stats["timeline"].append((code, mid))
        if i < total: time.sleep(BOILER_DELAY)
    return tasks, stats


# ════════════════════════════════════════════════════════════════
#  PHASE 2 — parallel downloads
# ════════════════════════════════════════════════════════════════

def _decompress_bz2(src, dest):
    try:
        d = bz2.BZ2Decompressor()
        with open(src, "rb") as fi, open(dest, "wb") as fo:
            while True:
                chunk = fi.read(CHUNK_SIZE)
                if not chunk: break
                fo.write(d.decompress(chunk))
        return True, ""
    except Exception as e:
        return False, str(e)

# ◄◄◄ FIXED — return _SKIPPED instead of True when file already exists
def _dl_one(task, dl_path, known_ids, log=print, progress_cb=None):
    mid, oid, name, url = task["mid"], task["oid"], task["name"], task["url"]
    final = dl_path / f"{name}.dem"

    # Check exact file AND any file with same match id, recursively
    mid_str = f"match730_{mid:021d}_"
    mid_plain = str(mid)  # without zero-padding, for tools that don't zero-pad
    already = (
        final.exists()
        or _is_present(known_ids, mid, oid)
        or any(dl_path.rglob(f"{mid_str}*.dem"))        # zero-padded, any subfolder
        or any(dl_path.rglob(f"*{mid_plain}*.dem"))     # non-padded, any subfolder
    )
    if already:
        _register(known_ids, mid, oid)
        return True, _SKIPPED

    is_bz2 = url.endswith(".bz2")
    suffix = ".dem.bz2" if is_bz2 else ".dem"
    tmp = dl_path / f"_tmp_{name}{suffix}"

    def on_prog(dl, total):
        if progress_cb: progress_cb(name, dl, total, "downloading")

    ok, err = _http_download(url, tmp, on_progress=on_prog)
    if not ok:
        tmp.unlink(missing_ok=True)
        if progress_cb: progress_cb(name, 0, 0, "failed")
        # 5xx = Valve CDN blip, cursor was not advanced, will retry next run
        if any(code in err for code in ("502", "503", "504")):
            return False, f"Valve CDN error (retry next run): {err[:60]}"
        return False, f"Download failed: {err[:80]}"

    verr = _validate_file(tmp, is_bz2)
    if verr:
        tmp.unlink(missing_ok=True)
        if progress_cb: progress_cb(name, 0, 0, "failed")
        return False, verr

    if is_bz2:
        # Signal extraction phase — file is downloaded but NOT done yet
        if progress_cb: progress_cb(name, 0, 0, "extracting")
        dem = dl_path / f"_tmp_{name}.dem"
        ok, berr = _decompress_bz2(tmp, dem)
        tmp.unlink(missing_ok=True)
        if not ok:
            dem.unlink(missing_ok=True)
            if progress_cb: progress_cb(name, 0, 0, "failed")
            return False, f"bz2: {berr[:80]}"
        dem.rename(final)
    else:
        tmp.rename(final)

    _register(known_ids, mid, oid)
    # Only NOW is the demo truly done — no tmp files, fully extracted
    if progress_cb: progress_cb(name, 0, 0, "done")
    return True, ""

# ◄◄◄ FIXED — separate skipped from actually downloaded
def download_all(tasks, dl_path, known_ids, log=print, progress_cb=None):
    if not tasks: return [], []
    n = len(tasks)
    workers = min(DOWNLOAD_WORKERS, n)
    log(f"\n[⬇]  {n} demo(s) — {workers} workers\n")
    downloaded, skipped, errors = [], [], []

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_dl_one, t, dl_path, known_ids, log, progress_cb): t
                for t in tasks}
        for f in as_completed(futs):
            task = futs[f]
            short = ("…" + task["name"][-40:]) if len(task["name"]) > 41 else task["name"]
            try:
                ok, reason = f.result()
                if ok and reason == _SKIPPED:
                    skipped.append(task)
                    log(f"  [⏭] {short}  (already on disk)")
                elif ok:
                    downloaded.append(task)
                    log(f"  [✓] {short}")
                else:
                    errors.append((short, reason))
                    log(f"  [✗] {short} — {reason[:60]}")
            except Exception as e:
                errors.append((short, str(e)))
                log(f"  [✗] {short} — {e}")

    if skipped:
        log(f"\n  [⏭] {len(skipped)} already on disk (not re-downloaded)")
    if errors:
        reasons: Dict[str, List[str]] = {}
        for nm, reason in errors:
            reasons.setdefault(reason, []).append(nm)
        log(f"\n  [📋] {len(errors)} failure(s):")
        for reason, names in reasons.items():
            log(f"\n    ▸ {reason}  ({len(names)})")
            for nm in names[:5]: log(f"      — {nm}")
            if len(names) > 5: log(f"      … +{len(names)-5}")
    return downloaded, skipped


# ════════════════════════════════════════════════════════════════
#  SCAN ENGINE
# ════════════════════════════════════════════════════════════════

# ◄◄◄ FIXED — use downloaded vs skipped properly for cursor tracking
def run_scan(cfg, dl_path, log=print, progress_cb=None):
    players = cfg.get("players", [])
    if not players: log("\n  [!] No players — add one first."); return
    boiler = ensure_boiler(log)
    if not boiler: log("\n  [✗] boiler-writter unavailable."); return
    log(f"\n[⚙]  Boiler: {boiler}")
    log("[⚠]  Steam must be running. CS2 must be CLOSED.\n")

    known_ids = _scan_folder(dl_path)
    cleaned = _cleanup_tmp(dl_path)
    if cleaned: log(f"[🧹]  {cleaned} temp file(s) removed")
    log(f"[📂]  {len(known_ids)} demo(s) already indexed.\n")

    all_tasks, task_player, player_timeline, seen_mids = [], {}, {}, set()

    for player in players:
        pid = player["steam_id"]
        log(f"{'═'*56}\n  {player['name']}  ({pid})\n{'═'*56}")
        codes = collect_codes(player, known_ids, log)
        if not codes: log("  [i] Nothing new.\n"); continue
        tasks, stats = resolve_urls(codes, boiler, known_ids, dl_path, log)

        unique, dupes = [], 0
        for t in tasks:
            ms = str(t["mid"])
            if ms in seen_mids: dupes += 1; continue
            seen_mids.add(ms); unique.append(t)

        parts = []
        if unique:            parts.append(f"{len(unique)} to DL")
        if dupes:             parts.append(f"{dupes} already queued")
        if stats["expired"]:  parts.append(f"{stats['expired']} expired")
        if stats["skipped"]:  parts.append(f"{stats['skipped']} already present")
        if stats["errors"]:   parts.append(f"{stats['errors']} error(s)")
        log(f"  [Σ] {' | '.join(parts) if parts else 'nothing'}")
        if stats["expired"] and not stats["resolved"]:
            log("  [⚠]  All expired — update share code.\n")
        player_timeline[pid] = stats["timeline"]
        for t in unique: task_player[t["name"]] = player
        all_tasks.extend(unique); log("")

    downloaded, skipped = download_all(all_tasks, dl_path, known_ids, log, progress_cb)

    # ◄◄◄ FIXED — advance the cursor only up to the last CONTIGUOUS success.
    # Jumping to the newest downloaded demo used to skip over matches that
    # failed mid-chain (Valve CDN 502), so they were never retried.
    ok_mids = {t["mid"] for t in downloaded} | {t["mid"] for t in skipped}
    for p in players:
        cursor = None
        for code, state in player_timeline.get(p["steam_id"], []):
            done = state is True or (state is not False and state in ok_mids)
            if not done: break          # stop at the first hole in the chain
            cursor = code
        if cursor: p["last_known_code"] = cursor

    save_config(cfg)
    log(f"\n{'═'*56}")
    log(f"  Done — {len(downloaded)} downloaded, {len(skipped)} skipped, "
        f"{len(all_tasks) - len(downloaded) - len(skipped)} failed")
    log(f"  {dl_path}")
    log(f"{'═'*56}")


# ════════════════════════════════════════════════════════════════
#  GUI — PLAYER DIALOG
# ════════════════════════════════════════════════════════════════

class PlayerDialog(tk.Toplevel):
    FIELDS = [
        ("name",            "Nickname:",          ""),
        ("steam_id",        "SteamID64:",         ""),
        ("api_key",         "Steam API Key:",     ""),
        ("auth_code",       "Auth Code:",         ""),
        ("last_known_code", "Share Code (start):",""),
    ]
    LINKS = {
        "api_key":         ("Get yours here",
                            "https://steamcommunity.com/dev/apikey"),
        "auth_code":       ("Get yours here",
                            "https://help.steampowered.com/en/wizard/HelpWithGameIssue/?appid=730&issueid=128"),
        "last_known_code": ("Same page as Auth Code",
                            "https://help.steampowered.com/en/wizard/HelpWithGameIssue/?appid=730&issueid=128"),
    }

    def __init__(self, parent, title="Player", data=None):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=PAL["bg"])
        self.resizable(False, False)
        self.result = None

        d = data or {}
        self.entries = {}

        SECRET_FIELDS = {"api_key", "auth_code"}

        for i, (key, label, _) in enumerate(self.FIELDS):
            val = d.get(key, d.get("oldest_share_code", "")) if key == "last_known_code" and not d.get(key) else d.get(key, "")

            lbl = tk.Label(self, text=label, bg=PAL["bg"], fg=PAL["fg"])
            lbl.grid(row=i, column=0, sticky="e", padx=(10, 5), pady=4)

            is_secret = key in SECRET_FIELDS
            e = tk.Entry(self, width=45, bg=PAL["alt_bg"], fg=PAL["fg"],
                         insertbackground=PAL["fg"], relief="flat",
                         highlightthickness=1, highlightcolor=PAL["accent"],
                         highlightbackground=PAL["btn_bg"],
                         show=("•" if is_secret else ""))
            e.insert(0, val)
            e.grid(row=i, column=1, padx=(0, 5), pady=4)
            self.entries[key] = e

            if is_secret:
                btn = tk.Button(self, text="👁", width=2, relief="flat",
                                bg=PAL["btn_bg"], fg=PAL["fg"],
                                activebackground=PAL["btn_hov"], cursor="hand2")
                btn.config(command=lambda entry=e, b=btn: (
                    entry.config(show="" if entry.cget("show") else "•"),
                    b.config(text="🙈" if not entry.cget("show") else "👁")
                ))
                btn.grid(row=i, column=3, padx=(2, 0), pady=4)

            link = self.LINKS.get(key)
            if link:
                lnk = tk.Label(self, text=link[0], bg=PAL["bg"],
                               fg=PAL["link"], cursor="hand2",
                               font=("TkDefaultFont", 9, "underline"))
                url = link[1]
                lnk.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))
                lnk.grid(row=i, column=2, sticky="w", padx=(0, 10))

        bf = tk.Frame(self, bg=PAL["bg"])
        bf.grid(row=len(self.FIELDS), column=0, columnspan=3, pady=10)

        for text, cmd in [("OK", self._ok), ("Cancel", self.destroy)]:
            tk.Button(bf, text=text, command=cmd, width=12,
                      bg=PAL["btn_bg"], fg=PAL["fg"],
                      activebackground=PAL["btn_hov"], activeforeground=PAL["fg"],
                      relief="flat", cursor="hand2"
                      ).pack(side="left", padx=5)

        self.entries["name"].focus_set()
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.transient(parent)
        self.grab_set()
        self.wait_window()

    def _ok(self):
        data = {k: e.get().strip() for k, e in self.entries.items()}

        if not _RE_STEAMID.match(data.get("steam_id", "")):
            messagebox.showerror("Error", "Invalid SteamID64 (17 digits).", parent=self)
            return
        if not data.get("api_key"):
            messagebox.showerror("Error", "API Key is required.", parent=self)
            return
        if not data.get("auth_code"):
            messagebox.showerror("Error", "Auth Code is required.", parent=self)
            return
        if not _valid_share_code(data.get("last_known_code", "")):
            messagebox.showerror("Error", "Invalid share code.", parent=self)
            return

        self.result = data
        self.destroy()


# ════════════════════════════════════════════════════════════════
#  GUI — SHARE CODE RESET DIALOG
# ════════════════════════════════════════════════════════════════

class ResetCodeDialog(tk.Toplevel):
    def __init__(self, parent, player_name, current, initial):
        super().__init__(parent)
        self.title("Reset Share Code")
        self.configure(bg=PAL["bg"])
        self.resizable(False, False)
        self.result = None

        tk.Label(self, text=f"Player: {player_name}", bg=PAL["bg"], fg=PAL["fg"],
                 font=("TkDefaultFont", 10, "bold")).pack(padx=15, pady=(10, 5))
        tk.Label(self, text=f"Current: {current}", bg=PAL["bg"], fg=PAL["fg"]).pack(padx=15)
        tk.Label(self, text=f"Initial: {initial}", bg=PAL["bg"], fg=PAL["fg"]).pack(padx=15, pady=(0, 10))
        tk.Label(self, text="New share code:", bg=PAL["bg"], fg=PAL["fg"]).pack(padx=15)

        self.entry = tk.Entry(self, width=45, bg=PAL["alt_bg"], fg=PAL["fg"],
                              insertbackground=PAL["fg"], relief="flat",
                              highlightthickness=1, highlightcolor=PAL["accent"],
                              highlightbackground=PAL["btn_bg"])
        self.entry.pack(padx=15, pady=5)
        self.entry.focus_set()

        bf = tk.Frame(self, bg=PAL["bg"])
        bf.pack(pady=10)
        for text, cmd in [("OK", self._ok), ("Cancel", self.destroy)]:
            tk.Button(bf, text=text, command=cmd, width=12,
                      bg=PAL["btn_bg"], fg=PAL["fg"],
                      activebackground=PAL["btn_hov"], activeforeground=PAL["fg"],
                      relief="flat", cursor="hand2"
                      ).pack(side="left", padx=5)

        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.transient(parent)
        self.grab_set()
        self.wait_window()

    def _ok(self):
        code = self.entry.get().strip()
        if not _valid_share_code(code):
            messagebox.showerror("Error", "Invalid share code.", parent=self)
            return
        self.result = code
        self.destroy()


# ════════════════════════════════════════════════════════════════
#  GUI — MAIN APPLICATION
# ════════════════════════════════════════════════════════════════

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        _apply_theme(self)
        self.title(f"CS2 Demo Downloader v{__version__}")
        self.geometry("920x660")
        self.minsize(750, 500)

        self.cfg = load_config()
        self.dl_path = self._init_dl_path()
        self._busy = False
        self._last_progress_update = 0.0
        self._dl_progress = {}
        self._dl_progress_lock = threading.Lock()

        self._build_ui()
        self._log("[✓]  Self-test OK")
        self._log(f"[📁]  Folder: {self.dl_path}")
        if not HAS_REQUESTS:
            self._log("[⚠]  pip install requests (recommended)")
        self._refresh_players()

    def _init_dl_path(self):
        cur = self.cfg.get("download_path", "")
        if cur and Path(cur).is_dir(): return Path(cur)
        p = Path.home() / "CS2_Demos"
        p.mkdir(parents=True, exist_ok=True)
        self.cfg["download_path"] = str(p); save_config(self.cfg)
        return p

    # ── BUILD UI ─────────────────────────────────────────────

    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=(10, 0))
        ttk.Label(top, text="📁 Download Folder:").pack(side="left")
        self.folder_var = tk.StringVar(value=str(self.dl_path))
        ttk.Label(top, textvariable=self.folder_var,
                  foreground=PAL["link"]).pack(side="left", padx=(5, 10))
        ttk.Button(top, text="Change…", command=self._change_folder).pack(side="left")

        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=10, pady=10)

        left = ttk.Frame(pane, width=270)
        pane.add(left, weight=0)

        plf = ttk.LabelFrame(left, text="Players")
        plf.pack(fill="both", expand=True, padx=(0, 5))

        self.player_list = tk.Listbox(
            plf, height=8, font=("Consolas", 10),
            bg=PAL["alt_bg"], fg=PAL["fg"],
            selectbackground=PAL["sel_bg"], selectforeground=PAL["sel_fg"],
            highlightthickness=0, relief="flat")
        self.player_list.pack(fill="both", expand=True, padx=5, pady=5)

        pb = ttk.Frame(plf)
        pb.pack(fill="x", padx=5, pady=(0, 5))
        ttk.Button(pb, text="+ Add",    command=self._add_player,    width=8).pack(side="left", padx=2)
        ttk.Button(pb, text="✏ Edit",   command=self._edit_player,   width=8).pack(side="left", padx=2)
        ttk.Button(pb, text="✗ Remove", command=self._remove_player, width=8).pack(side="left", padx=2)

        af = ttk.LabelFrame(left, text="Actions")
        af.pack(fill="x", padx=(0, 5), pady=(10, 0))

        self.scan_btn = ttk.Button(
            af, text="▶  Scan & Download",
            style="Action.TButton", command=self._scan)
        self.scan_btn.pack(fill="x", padx=8, pady=(8, 4))

        for txt, cmd in [
            ("🔄  Reset Share Code",   self._reset_code),
            ("🔧  Test GC Connection", self._test_gc),
            ("📦  Reinstall Boiler",    self._reinstall_boiler),
        ]:
            ttk.Button(af, text=txt, command=cmd).pack(fill="x", padx=8, pady=2)

        ttk.Frame(af).pack(pady=4)

        right = ttk.Frame(pane)
        pane.add(right, weight=1)

        self.log_text = tk.Text(
            right, wrap="word", font=("Consolas", 9),
            bg=PAL["bg"], fg=PAL["fg"], insertbackground=PAL["fg"],
            state="disabled", relief="flat",
            highlightthickness=1, highlightbackground=PAL["btn_bg"])
        scroll = ttk.Scrollbar(right, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)

        self.log_text.tag_configure("ok",   foreground=PAL["ok"])
        self.log_text.tag_configure("fail", foreground=PAL["fail"])
        self.log_text.tag_configure("warn", foreground=PAL["warn"])
        self.log_text.tag_configure("info", foreground=PAL["info"])
        self.log_text.tag_configure("timestamp", foreground="#6a6a6a")

        bot = ttk.Frame(self)
        bot.pack(fill="x", padx=10, pady=(0, 10))

        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(
            bot, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill="x", side="left", expand=True, padx=(0, 10))

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(bot, textvariable=self.status_var).pack(side="right")

    # ── LOGGING (thread-safe) ────────────────────────────────

    def _log(self, msg, tag=None):
        stamp = datetime.now().strftime("[%H:%M:%S] ")

        def _do():
            self.log_text.configure(state="normal")
            t = tag
            if not t:
                if   "[✓]" in msg: t = "ok"
                elif "[✗]" in msg: t = "fail"
                elif "[⚠]" in msg or "[⌛]" in msg: t = "warn"
                elif "═" in msg: t = "info"
            for line in msg.split("\n"):
                if line.strip():
                    self.log_text.insert("end", stamp, "timestamp")
                    self.log_text.insert("end", line + "\n", t or ())
                else:
                    self.log_text.insert("end", "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        if threading.current_thread() is threading.main_thread():
            _do()
        else:
            self.after(0, _do)

    # ── PROGRESS (thread-safe) ───────────────────────────────

    def _progress_cb(self, name, dl, total, phase="downloading"):
        with self._dl_progress_lock:
            if phase == "downloading":
                if total <= 0:
                    return
                self._dl_progress[name] = (dl, total, "downloading")
            elif phase == "extracting":
                # Bytes are fully downloaded; keep totals, change phase
                prev = self._dl_progress.get(name)
                if prev:
                    self._dl_progress[name] = (prev[1], prev[1], "extracting")
                else:
                    return
            elif phase in ("done", "failed"):
                prev = self._dl_progress.get(name)
                if prev:
                    self._dl_progress[name] = (prev[1], prev[1], phase)
                else:
                    return

            agg_dl    = sum(d for d, _, _ in self._dl_progress.values())
            agg_total = sum(t for _, t, _ in self._dl_progress.values())
            snapshot  = list(self._dl_progress.values())

        now = time.monotonic()
        # Throttle only the high-frequency "downloading" updates
        if phase == "downloading" and (now - self._last_progress_update) < 0.1:
            return
        self._last_progress_update = now

        pct = agg_dl * 100 / agg_total if agg_total > 0 else 0

        done_count       = sum(1 for _, _, p in snapshot if p == "done")
        extracting_count = sum(1 for _, _, p in snapshot if p == "extracting")
        active_count     = sum(1 for d, t, p in snapshot if p == "downloading" and d < t)
        failed_count     = sum(1 for _, _, p in snapshot if p == "failed")

        parts = []
        if active_count:     parts.append(f"{active_count} downloading")
        if extracting_count: parts.append(f"{extracting_count} extracting")
        if done_count:       parts.append(f"{done_count} done")
        if failed_count:     parts.append(f"{failed_count} failed")
        status_text = ", ".join(parts) if parts else "processing"
        msg = f"⬇ {status_text}  ({agg_dl >> 20}/{agg_total >> 20} MB)"

        self.after(0, lambda p=pct, m=msg: (
            self.progress_var.set(p),
            self.status_var.set(m),
        ))

    # ── PLAYER MANAGEMENT ────────────────────────────────────

    def _refresh_players(self):
        def _do():
            self.player_list.delete(0, "end")
            for p in self.cfg.get("players", []):
                self.player_list.insert("end", f"{p['name']}  ({p['steam_id']})")
        if threading.current_thread() is threading.main_thread():
            _do()
        else:
            self.after(0, _do)

    def _selected_index(self):
        sel = self.player_list.curselection()
        if not sel:
            messagebox.showinfo("Info", "Select a player first.", parent=self)
            return None
        return sel[0]

    def _add_player(self):
        dlg = PlayerDialog(self, "Add Player")
        if dlg.result:
            d = dlg.result
            self.cfg.setdefault("players", []).append({
                "name": d["name"] or d["steam_id"],
                "steam_id": d["steam_id"], "api_key": d["api_key"],
                "auth_code": d["auth_code"],
                "oldest_share_code": d["last_known_code"],
                "last_known_code": d["last_known_code"],
            })
            save_config(self.cfg); self._refresh_players()
            self._log(f"  [✓] Added: {d['name'] or d['steam_id']}", "ok")

    def _edit_player(self):
        idx = self._selected_index()
        if idx is None: return
        p = self.cfg["players"][idx]
        dlg = PlayerDialog(self, "Edit Player", p)
        if dlg.result:
            d = dlg.result
            p["name"]            = d["name"] or p["name"]
            p["steam_id"]        = d["steam_id"]
            p["api_key"]         = d["api_key"]
            p["auth_code"]       = d["auth_code"]
            p["last_known_code"] = d["last_known_code"]
            save_config(self.cfg); self._refresh_players()
            self._log(f"  [✓] Updated: {p['name']}", "ok")

    def _remove_player(self):
        idx = self._selected_index()
        if idx is None: return
        p = self.cfg["players"][idx]
        if messagebox.askyesno("Confirm", f"Remove {p['name']}?", parent=self):
            self.cfg["players"].pop(idx)
            save_config(self.cfg); self._refresh_players()
            self._log(f"  [✓] Removed: {p['name']}", "ok")

    def _reset_code(self):
        idx = self._selected_index()
        if idx is None: return
        p = self.cfg["players"][idx]
        dlg = ResetCodeDialog(
            self, p["name"],
            p.get("last_known_code", "—"),
            p.get("oldest_share_code", "—"))
        if dlg.result:
            p["last_known_code"] = dlg.result
            p["oldest_share_code"] = dlg.result
            save_config(self.cfg)
            self._log(f"  [✓] Share code reset for {p['name']}: {dlg.result}", "ok")

    # ── FOLDER ────────────────────────────────────────────────

    def _change_folder(self):
        folder = filedialog.askdirectory(
            initialdir=str(self.dl_path), title="Select download folder",
            parent=self)
        if folder:
            self.dl_path = Path(folder)
            self.dl_path.mkdir(parents=True, exist_ok=True)
            self.cfg["download_path"] = str(self.dl_path)
            save_config(self.cfg)
            self.folder_var.set(str(self.dl_path))
            self._log(f"[📁]  Folder: {self.dl_path}")

    # ── THREADED ACTIONS ──────────────────────────────────────

    def _set_busy(self, busy):
        self._busy = busy
        self.scan_btn.configure(state="disabled" if busy else "normal")
        if not busy:
            self.after(0, lambda: self.status_var.set("Ready"))
            self.after(0, lambda: self.progress_var.set(0))

    def _run_threaded(self, fn):
        if self._busy:
            messagebox.showinfo("Busy", "An operation is already running.", parent=self)
            return
        self._set_busy(True)
        def worker():
            try:
                fn()
            except Exception as e:
                self._log(f"\n  [✗] Error: {e}", "fail")
            finally:
                self.after(0, lambda: self._set_busy(False))
        threading.Thread(target=worker, daemon=True).start()

    def _scan(self):
        def do():
            self._dl_progress.clear()
            self.after(0, lambda: self.status_var.set("Scanning…"))
            run_scan(self.cfg, self.dl_path, log=self._log, progress_cb=self._progress_cb)
            # ◄◄◄ Final safety net — make sure every _tmp_ file is gone
            #     before the worker returns and status becomes "Ready"
            remaining = _cleanup_tmp(self.dl_path)
            if remaining:
                self._log(f"[🧹]  {remaining} leftover temp file(s) cleaned")
            self._refresh_players()
        self._run_threaded(do)

    def _test_gc(self):
        def do():
            boiler = ensure_boiler(self._log)
            if boiler:
                self._log(f"\n[⚙]  Boiler: {boiler}")
                _boiler_test(boiler, self._log)
        self._run_threaded(do)

    def _reinstall_boiler(self):
        def do():
            self._log("\n[📦]  Reinstalling boiler-writter…")
            if BOILER_DIR.exists(): shutil.rmtree(BOILER_DIR)
            b = install_boiler(self._log)
            if not b: self._log("  [✗] Installation failed.", "fail")
        self._run_threaded(do)


# ════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════

def main():
    assert len(SHARECODE_ALPHABET) == 57, "Alphabet corrupted"
    _selftest()
    app = App()
    app.mainloop()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("Startup Error", str(e))
            root.destroy()
        except Exception:
            print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)