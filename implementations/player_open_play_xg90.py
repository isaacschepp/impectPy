import os
import sys
import pandas as pd
from typing import Optional, Tuple
from datetime import datetime

# Allow running from the repo root
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, REPO_ROOT)

from impectPy import Impect, getPlayerOpenPlayXG90


def load_env():
    """Load environment variables from a .env file in the repo root if available."""
    try:
        from dotenv import load_dotenv  # type: ignore
        env_path = os.path.join(REPO_ROOT, '.env')
        if os.path.exists(env_path):
            load_dotenv(env_path)
    except ImportError:
        # python-dotenv not installed; skip silently
        pass


def get_windows_credentials(target_hint: Optional[str] = None) -> Optional[Tuple[str, str, str]]:
    """Try to read username/password from Windows Credential Manager.

    - If IMPECT_CRED_TARGET is set, use that target explicitly.
    - Else enumerate credentials and pick one whose target contains 'impect'.
    Returns (username, password, target) or None if not available.
    """
    explicit_target = os.getenv('IMPECT_CRED_TARGET') or target_hint

    # Try pywin32 win32cred first (best coverage of Windows CredMan)
    try:
        import win32cred  # type: ignore

        def _decode_blob(blob) -> Optional[str]:
            if blob is None:
                return None
            if isinstance(blob, bytes):
                for enc in ("utf-16le", "utf-8", "latin-1"):
                    try:
                        s = blob.decode(enc)
                        return s.replace("\x00", "").strip()
                    except Exception:
                        continue
                return None
            if isinstance(blob, str):
                return blob.replace("\x00", "").strip()
            return None

        def read_target(target: str):
            try:
                cred = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC)
                username = cred.get('UserName')
                password = _decode_blob(cred.get('CredentialBlob'))
                if username and password:
                    return username, password, target
            except Exception:
                return None
            return None

        if explicit_target:
            res = read_target(explicit_target)
            if res:
                return res

        # Enumerate and search for likely impect entries
        try:
            creds = win32cred.CredEnumerate(None, 0)
            # Prefer more direct targets first
            preferred = []
            others = []
            for c in creds:
                target_name = (c.get('TargetName') or '')
                t_lower = target_name.lower()
                if any(k in t_lower for k in ("login.impect.com", "api.impect.com")):
                    preferred.append(target_name)
                elif 'impect' in t_lower:
                    others.append(target_name)
            if os.getenv('IMPECT_LIST_CREDENTIALS'):
                if preferred or others:
                    print("Discovered Credential Manager targets containing 'impect':")
                    for t in preferred + others:
                        print(f" - {t}")
                else:
                    print("No Credential Manager targets containing 'impect' were found.")
            for t in preferred + others:
                res = read_target(t)
                if res:
                    return res
        except Exception:
            pass
    except ImportError:
        pass

    # Fallback: keyring (requires 'keyring' to be installed and username known)
    try:
        import keyring  # type: ignore
        username = os.getenv('IMPECT_USERNAME')
        if username:
            password = keyring.get_password('impect', username)
            if password:
                return username, password, 'keyring:impect'
    except ImportError:
        pass

    return None

# Contract
# - Input: iteration id (int), positions list (list[str]), token via env IMPECT_TOKEN or via login
# - Output: prints a table of player open-play xG per 90 for the iteration
# - Error cases: no matches/events; missing token; invalid iteration


def main():
    # Load .env first (if present)
    load_env()

    iteration = int(os.getenv('IMPECT_ITERATION', '1236'))
    positions = ["CENTER_FORWARD"]  # CF only as requested

    token = os.getenv('IMPECT_TOKEN')
    username = os.getenv('IMPECT_USERNAME')
    password = os.getenv('IMPECT_PASSWORD')

    api = Impect()
    if token:
        api.init(token)
    else:
        # If user/pass not set, try Windows Credential Manager
        if not (username and password):
            creds = get_windows_credentials()
            if creds:
                username, password, target_used = creds
                print(f"Using Windows credentials from target: {target_used}")

        if username and password:
            print(f"Attempting login as: {username}")
            try:
                token = api.login(username, password)
            except Exception as e:
                # Provide guidance for 401 Unauthorized
                msg = str(e)
                if '401' in msg or 'Unauthorized' in msg:
                    print("Login failed (401 Unauthorized).")
                    print("Tips:")
                    print(" - Make sure the Windows Credential stores the API username and the correct password.")
                    print(" - If multiple creds exist, set IMPECT_CRED_TARGET to the exact Credential Manager entry.")
                    print(" - Alternatively, set IMPECT_TOKEN env var to bypass username/password.")
                    print(" - To see candidate targets, set IMPECT_LIST_CREDENTIALS=1 and re-run the script.")
                raise
        else:
            print(
                "Missing credentials. Set IMPECT_TOKEN or IMPECT_USERNAME/IMPECT_PASSWORD,\n"
                "or store them in Windows Credential Manager. Optional: set IMPECT_CRED_TARGET to the\n"
                "exact Credential Manager target. If needed, install pywin32 (win32cred) or keyring to auto-read."
            )
            sys.exit(1)

    # Use the exported helper function
    df = getPlayerOpenPlayXG90(iteration=iteration, positions=positions, token=token)

    # Keep only the essential columns
    cols = [
        'iterationId', 'competitionName', 'season', 'squadId', 'squadName',
        'playerId', 'playerName', 'positions', 'matchShare', 'playDuration',
        'openPlayXG', 'openPlayXG90'
    ]
    cols = [c for c in cols if c in df.columns]
    df = df[cols].sort_values(['openPlayXG90'], ascending=False)

    # Display top 25
    pd.set_option('display.max_columns', None)
    print(df.head(25).to_string(index=False))

    # Export full list to CSV
    output_dir = os.getenv('IMPECT_OUTPUT_DIR', os.path.join(REPO_ROOT, 'implementations', 'output'))
    try:
        os.makedirs(output_dir, exist_ok=True)
    except Exception:
        pass
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    pos_slug = '-'.join(positions)
    out_path = os.path.join(output_dir, f'player_open_play_xg90_iter_{iteration}_{pos_slug}_{ts}.csv')
    try:
        df.to_csv(out_path, index=False)
        print(f"Saved full results to: {out_path}")
    except Exception as e:
        print(f"Warning: could not write CSV to {out_path}: {e}")


if __name__ == '__main__':
    main()
