Implementations

This folder contains small, ready-to-run examples built on top of the impectPy API wrapper.

- player_open_play_xg90.py: Compute open-play, non-penalty xG per 90 for players in a given iteration, filtered by positions.

Credentials
- Copy `.env.example` to `.env` in the repo root and set IMPECT_USERNAME/IMPECT_PASSWORD or IMPECT_TOKEN.
- The script loads `.env` automatically if `python-dotenv` is installed.
- You can also store credentials in Windows Credential Manager; set IMPECT_CRED_TARGET if multiple entries exist.
