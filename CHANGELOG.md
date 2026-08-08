# Changelog

All notable changes to this project are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [3.3.3] - 2026-08-08

### Fixed
- **Cursor could skip a failed match forever.** The per-player share-code cursor
  used to advance to the newest *successfully downloaded* demo, jumping clean
  over any match that failed earlier in the same chain (e.g. a Valve CDN 502).
  Those matches were silently lost — the "retry next run" message never
  actually retried them. The cursor now advances only through an unbroken run
  of successes and stops at the first failure, so failed matches are
  re-attempted on the next scan. Applied to both the CLI and GUI.
- Replaced the unreliable ~6T "proximity" heuristic used to detect duplicate
  demos with an exact match on match ID / reservation ID (both 64-bit and
  32-bit forms) — the old distance threshold could misclassify genuinely
  distinct matches as duplicates.

### Security
- Verify the `boiler-writter` binary's SHA-256 before use.
- Drop the insecure `mktemp`-style temp file creation.
- Mask secret fields (API key, auth code) in the GUI.
- More precise process termination when stopping `boiler-writter`.
- Added `.gitignore` to keep local config/secrets out of the repository.

### Changed
- Extraction is now verified before the tool reports "done".

## [CS2-demo-fetcher] - 2026-04-02

Initial public release.
