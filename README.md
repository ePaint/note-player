# note-player

A screen-capture autoplayer for frets-style rhythm games (think Guitar Hero). It watches
the note highway in real time, tracks every gem as a physical object descending the screen,
and sends synthetic key presses timed to land at the fret — taps, chords, thin sustains,
and arrow-lift holds included.

Current state on hard content: ~1.5% mistakes per note, 50-second flawless streaks at 4x
multiplier through dense seven-minute songs.

**Windows only** (screen capture via the Desktop Duplication API, key delivery via pywin32).

## Requirements

- Windows 10/11
- [uv](https://docs.astral.sh/uv/) — the only thing you install by hand:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Everything else (Python 3.9, all dependencies) is handled by uv automatically.

## Run it

```powershell
git clone https://github.com/ePaint/note-player.git
cd note-player
uv run bot.py
```

The first `uv run` creates the environment and installs everything, then starts the bot.
After that it starts instantly.

While it runs:

| Key | Action |
|-----|--------|
| **Caps Lock** | Toggle autoplay on/off (starts OFF — press once to start) |
| **p** | Quit the bot |

Start the bot, switch to the game, pick a song, and let it play.

## Game setup

- Run the game in **windowed fullscreen**.
- Prefer the **low-quality rendering mode** — it keeps in-game brightness close to what
  the bundled match images expect.
- Built-in match images cover **1440p** (primary, most tested), **1080p**, and **768p**.
  Other resolutions need their own images following the naming scheme in `assets/`.

## Configuration

Player-facing options live in [user_settings.toml](user_settings.toml). Restart the bot
after editing.

| Key | Meaning |
|-----|---------|
| `key_1` … `key_5` | The five lane keys (default `d f j k l`) |
| `exit_key` | Quit key (default `p`) |
| `always_single_lanemode`, `single_lanemode_lanes` | Lane count (5 = expert) |
| `use_white_tile` / `use_diamond_tile` / `use_battlestage_tile` | Match your in-game note skin |

The engine's tuned internals (timings, detection thresholds, capture geometry) ship as
in-code defaults — you normally never touch them. Any advanced key can still be
overridden by adding it to user_settings.toml, which always wins.

## Diagnostics

For debugging a bad run, add these two lines to user_settings.toml — the run then writes
captured frames and a full per-frame detection log into `diag/` (roughly 0.5 GB per
minute). Remove them for normal play.

```toml
diagnostic_capture = true
log_detection = true
```

Step-by-step instructions (including how to package a recording to send in) are in
[DEBUGGING.md](DEBUGGING.md).

## License

AGPL-3.0 — see [LICENSE](LICENSE).

**This project is for educational purposes only.**
