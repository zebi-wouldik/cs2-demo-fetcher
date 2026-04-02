# CS2 Demo Fetcher

A desktop tool that automatically retrieves your CS2 matchmaking demos — the same way [Leetify](https://leetify.com) and (in some ways) [CS:DM](https://github.com/akiver/cs-demo-manager) do it.

![GUI Screenshot](https://img.shields.io/badge/GUI-tkinter-blue?style=flat-square)
![Python](https://img.shields.io/badge/python-3.10%2B-brightgreen?style=flat-square)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey?style=flat-square)

## Versions

There are two versions of the tool available:

- **GUI** — A graphical user interface for ease of use.
- **CLI** — A command-line interface for terminal/console usage.

Both versions share the same configuration file and behave identically under the hood.

## How It Works

```
Share Code → Steam API (chain to newer matches)
                ↓
         Decode share code → matchId + outcomeId + token
                ↓
         boiler-writter → Game Coordinator → demo URL
                ↓
         Fetch .dem.bz2 → validate → decompress → match730_XXX_YYY.dem
```

1. **Share code chaining** — Starting from a known share code, the tool calls Steam's `GetNextMatchSharingCode` API to discover all newer matches
2. **Game Coordinator** — Uses [boiler-writter](https://github.com/akiver/boiler-writter) to communicate with Valve's Game Coordinator and retrieve the demo fetch URL
3. **Parallel transfers** — Retrieves up to 4 demos simultaneously, validates file integrity, and decompresses `.bz2` archives
4. **Deduplication** — Scans your demo folder to skip already-fetched matches, and deduplicates across players who shared the same match

## Features

- 🖥️ **GUI** — Clickable interface built with tkinter, no terminal needed
- 🔄 **Automatic boiler-writter installation** — Fetches the correct binary for your platform from GitHub
- 👥 **Multi-player support** — Track demos for multiple Steam accounts
- ⚡ **Parallel transfers** — 4 concurrent workers with retry and exponential backoff
- 🔍 **File validation** — Detects expired demos (HTML responses) and corrupted archives before decompression
- 🧹 **Orphan cleanup** — Removes leftover temp files from interrupted runs
- 🎨 **Color-coded log** — Green for success, red for errors, yellow for warnings
- 📊 **Progress tracking** — Real-time progress bar during transfers
- 🔒 **Atomic config writes** — No config corruption on crash
- 🛡️ **Safe archive extraction** — Protects against path traversal attacks

## 🛡️ Safety & Privacy

This tool operates entirely locally on your machine using your own Steam API key and match sharing codes to communicate directly with Valve's servers. It does not exfiltrate keys, track your usage, or send your data to any third-party services.

## Prerequisites

- **Python 3.10+**
- **Steam** must be running and logged in
- **CS2** must be **closed** (boiler-writter needs exclusive access to the Game Coordinator)

### Required credentials (per player)

| Credential | Where to get it |
|---|---|
| **Steam API Key** | [steamcommunity.com/dev/apikey](https://steamcommunity.com/dev/apikey) |
| **Auth Code** | [help.steampowered.com/en/wizard/HelpWithGameIssue/?appid=730&issueid=128](https://help.steampowered.com/en/wizard/HelpWithGameIssue/?appid=730&issueid=128) |
| **Share Code** | CS2 → Watch → Your Matches → Copy Share Link / or in the **Auth Code** page |
| **SteamID64** | [steamid.io](https://steamid.io/) |

## Installation

### Option 1: Clone and run

```bash
git clone https://github.com/zebi-wouldik/cs2-demo-fetcher.git
cd cs2-demo-fetcher
pip install -r requirements.txt
python cs2_demo_fetcher_CLI.py
```
There's also the GUI variant.

### Option 2: Extract from release

1. Extract the [latest release](https://github.com/zebi-wouldik/cs2-demo-fetcher/releases) to your desired location
2. Install dependencies: `pip install -r requirements.txt`
3. Run: `python cs2_demo_fetcher_CLI.py` or the _GUI variant.

> **Note:** `requests` is optional but recommended. The tool falls back to `urllib` if not installed, but with no streaming progress and slower transfers.

## Usage

### 1. Add a player

Click **+ Add** and fill in the form:

- **Nickname** — Display name (anything you want)
- **SteamID64** — 17-digit Steam ID
- **Steam API Key** — From the link above
- **Auth Code** — From the link above (format: `XXXX-XXXXX-XXXX`)
- **Share Code** — From your most recent CS2 match (format: `CSGO-XXXXX-XXXXX-XXXXX-XXXXX-XXXXX`)

### 2. Set demo folder

Click **Change…** next to the folder path and select where demos should be saved.

### 3. Scan & Retrieve

Click **▶ Scan & Retrieve**. The tool will:

1. Chain share codes to find all new matches per player
2. Resolve demo URLs via boiler-writter
3. Retrieve and decompress demos in parallel
4. Save progress so the next scan picks up where it left off

### Other actions

| Button | Description |
|---|---|
| **✏ Edit** | Modify a player's credentials |
| **✗ Remove** | Delete a player |
| **🔄 Reset Share Code** | Set a new starting share code for a player |
| **🔧 Test GC Connection** | Verify boiler-writter can reach the Game Coordinator |
| **📦 Reinstall Boiler** | Re-fetch boiler-writter (useful after updates) |

## File Structure

```
cs2-demo-fetcher/
├── cs2_demo_fetcher_CLI.py    # CLI script
├── cs2_demo_fetcher_GUI.py    # GUI SCRIPT
├── config.json               # Auto-generated config (players + settings)
├── requirements.txt
├── README.md
├── LICENSE
└── boiler/                   # Auto-fetched boiler-writter binary
    └── bin/
        └── boiler-writter.exe
```

## Demo Naming Convention

Retrieved demos follow the CSDM naming standard:

```
match730_003811164091523793407_466.dem
         └──── matchId ──────┘ └─┘
                          outcomeId (lower 32 bits)
```

This is compatible with [CS:DM](https://github.com/akiver/cs-demo-manager), [Leetify](https://leetify.com), and other analysis tools.

## How Deduplication Works

The tool scans all `.dem` files in your demo folder at startup and extracts the `matchId` from each filename. Any match already present is skipped — no database or index file needed. The folder itself is the source of truth.

Matches shared across multiple tracked players are retrieved only once.

## Limitations

- **30-day expiration** — Valve deletes demo files after ~30 days. Expired matches cannot be recovered by any tool.
- **Matchmaking only** — Only Valve MM demos (Premier, Competitive, Wingman). FACEIT/ESEA demos are not supported.
- **One Steam account on the machine** — boiler-writter connects through the locally running Steam client. It uses whichever account is currently logged in.
- **CS2 must be closed** — boiler-writter briefly launches CS2 in the background to communicate with the Game Coordinator.

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `Steam not running` | Steam client is not open | Launch Steam and log in |
| `User not logged in / GC busy` | Steam is open but GC is unresponsive | Close CS2, wait a few seconds, retry |
| `Match not found (expired > 30 days)` | Demo deleted by Valve | Nothing to do — update share code to a recent match |
| `HTML response (demo expired)` | URL works but Valve returns error page | Demo is expired server-side |
| `boiler-writter timeout` | GC is unreachable or CS2 is running | Close CS2 completely, retry |
| `WinError 32` | File locked by another process | Close any program using the demo folder, retry |
| No new matches found | `last_known_code` is already up to date | Play a new match or reset the share code |

## How to Get a New Share Code

1. Open **CS2**
2. Go to **Watch** → **Your Matches**
3. Click on any recent match
4. Click **Copy Share Link**
5. Paste into the tool (Add/Edit player, or Reset Share Code)

## Credits

- **[boiler-writter](https://github.com/akiver/boiler-writter)** by [akiver](https://github.com/akiver) — Game Coordinator communication
- **[csgo-sharecode](https://github.com/akiver/csgo-sharecode)** by [akiver](https://github.com/akiver) — Share code encoding/decoding reference
- **Valve** — Steam Web API and Game Coordinator protocol
