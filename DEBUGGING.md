# Recording a diagnostic run

When the bot misplays, a diagnostic recording lets us see exactly what it saw, frame by
frame. This takes about two minutes to set up. Please record **one full song** where the
problem happens.

## 1. Check disk space

A diagnostic run writes roughly **0.5 GB per minute** — keep **4-5 GB free** for a full
song. The recording covers up to ~7-8 minutes of play automatically, so any normal song
fits.

## 2. Turn diagnostics on

Open **user_settings.toml** (it is in the note-player folder, next to `bot.py`) with any
text editor and add these two lines at the bottom, exactly as written:

```toml
diagnostic_capture = true
log_detection = true
```

Save the file. **If the bot is running, close it and start it again** — settings are only
read at startup.

## 3. Verify it is actually recording

Start the bot from the note-player folder as usual:

```powershell
uv run bot.py
```

Look at the startup panel: the **Diagnostics** line must say **ON**. If it says `off`,
the settings did not load — see Troubleshooting below.

## 4. Record

Play the **whole song** where the problem happens, start to finish, then quit the bot
with the exit key (**P** by default).

Do not judge the recording by file sizes *while it runs* — Windows often shows 0 KB for
files that are still open. Check after you quit.

## 5. Send us the results

After quitting you will find, inside the note-player folder:

- a **`diag/`** folder (thousands of `.png` frames plus `detect.jsonl` and `mult.jsonl`)
- a **`logs/`** folder (a `note-player-....log` file per session)

Right-click the `diag/` folder → **Compress to ZIP file**, and send us:

1. the ZIP of `diag/`
2. the **newest** file from `logs/`
3. one sentence about what went wrong and roughly when in the song ("it drops the long
   holds in the chorus, about a minute in")

That last sentence is genuinely the most valuable part.

## 6. Turn diagnostics off again

Delete the two lines from user_settings.toml (or set them to `false`). Diagnostic mode
slows nothing noticeable, but it eats disk quickly — don't leave it on for normal play.
You can safely delete the `diag/` folder after sending it.

---

## Troubleshooting

**The Diagnostics line says `off` at startup**
- The two lines must be in **user_settings.toml** — not in any other file.
- Values must be bare `true` (no quotes, not `yes`, not `True` capitalized — though
  quoted `"true"` also works).
- You edited the file while the bot was running: restart the bot.

**No `diag/` folder appears**
- The folder is created **where you start the bot from**. Always run `uv run bot.py`
  from inside the note-player folder.
- Startup shows Diagnostics ON but the folder is missing after quitting: check disk
  space, and check that your antivirus is not quarantining the folder.

**`diag/` exists but the files look empty (0 KB)**
- Normal while the bot is running. Quit the bot first, then refresh the folder view.

**The song is longer than ~8 minutes**
- Add one more line to user_settings.toml: `diagnostic_max_frames = 20000`
  (that is roughly 11 minutes of frames).

**The bot crashes on startup after editing**
- A typo in the TOML file. Delete your added lines, save, confirm the bot starts, then
  re-add them exactly as shown above.
