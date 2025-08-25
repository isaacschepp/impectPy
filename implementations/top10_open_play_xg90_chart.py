import os
import sys
import io
import re
import math
import time
from typing import Optional, Tuple, Dict
from datetime import datetime

import pandas as pd

# Allow running from the repo root
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, REPO_ROOT)

from impectPy import Impect, getPlayerOpenPlayXG90  # type: ignore


def load_env():
    """Load environment variables from a .env file in the repo root if available."""
    try:
        from dotenv import load_dotenv  # type: ignore
        env_path = os.path.join(REPO_ROOT, '.env')
        if os.path.exists(env_path):
            load_dotenv(env_path)
    except ImportError:
        pass


def get_windows_credentials(target_hint: Optional[str] = None) -> Optional[Tuple[str, str, str]]:
    """Try to read username/password from Windows Credential Manager.

    - If IMPECT_CRED_TARGET is set, use that target explicitly.
    - Else enumerate credentials and pick one whose target contains 'impect'.
    Returns (username, password, target) or None if not available.
    """
    explicit_target = os.getenv('IMPECT_CRED_TARGET') or target_hint

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

        try:
            creds = win32cred.CredEnumerate(None, 0)
            preferred = []
            others = []
            for c in creds:
                target_name = (c.get('TargetName') or '')
                t_lower = target_name.lower()
                if any(k in t_lower for k in ("login.impect.com", "api.impect.com")):
                    preferred.append(target_name)
                elif 'impect' in t_lower:
                    others.append(target_name)
            for t in preferred + others:
                res = read_target(t)
                if res:
                    return res
        except Exception:
            pass
    except ImportError:
        pass

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


def ensure_libs(include_logos: bool):
    """Ensure required plotting libs are available; return imported modules."""
    try:
        import matplotlib.pyplot as plt  # type: ignore
        from matplotlib.offsetbox import OffsetImage, AnnotationBbox  # type: ignore
    except Exception as e:
        raise RuntimeError("matplotlib is required. Please install it: pip install matplotlib") from e

    Image = None
    requests = None
    if include_logos:
        try:
            from PIL import Image  # type: ignore
        except Exception as e:
            raise RuntimeError("Pillow is required for logos. Install it or set IMPECT_INCLUDE_LOGOS=0.") from e
        import requests  # type: ignore

    return plt, OffsetImage, AnnotationBbox, Image, requests


def _find_url_recursive(obj) -> Optional[str]:
    """Recursively search for a plausible logo URL in nested dict/list structures."""
    if isinstance(obj, dict):
        # Check preferred keys first
        for key in ("logoUrl", "logo", "crestUrl", "crest", "badgeUrl", "badge", "imageUrl", "image", "icon"):
            if key in obj and isinstance(obj[key], str) and obj[key].startswith("http"):
                return obj[key]
        # Then check any url-like fields mentioning logo-ish terms
        for k, v in obj.items():
            if isinstance(v, str) and v.startswith("http") and re.search(r"(logo|crest|badge|image|icon|emblem)", k, re.I):
                return v
        # Recurse into values
        for v in obj.values():
            res = _find_url_recursive(v)
            if res:
                return res
    elif isinstance(obj, list):
        for x in obj:
            res = _find_url_recursive(x)
            if res:
                return res
    return None


def detect_logo_url_fields(entry: Dict) -> Optional[str]:
    """Given a squad dict, try to find a logo/crest/badge URL field (deep search)."""
    return _find_url_recursive(entry)


def fetch_squad_logos(iteration: int, token: str, squad_ids: list[int]) -> Dict[int, Optional[str]]:
    """Fetch squad metadata for iteration and return mapping squadId -> logo URL (or None)."""
    import requests  # local import to avoid hard dep at import time

    url = f"https://api.impect.com/v5/customerapi/iterations/{iteration}/squads"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code != 200:
        print(f"Warning: squads endpoint returned {resp.status_code}; proceeding without logos.")
        return {sid: None for sid in squad_ids}
    data = resp.json().get("data", [])

    logo_by_id: Dict[int, Optional[str]] = {}
    index: Dict[int, Dict] = {}
    for entry in data:
        try:
            sid = int(entry.get("id"))
            index[sid] = entry
        except Exception:
            continue

    for sid in squad_ids:
        entry = index.get(int(sid))
        logo_url = detect_logo_url_fields(entry) if entry else None
        logo_by_id[int(sid)] = logo_url

    return logo_by_id


def save_logo(logo_url: str, squad_id: int, requests_mod, Image_mod) -> Optional[str]:
    """Download and store a logo to assets folder, return local path or None."""
    assets_dir = os.path.join(REPO_ROOT, 'implementations', 'assets', 'logos')
    os.makedirs(assets_dir, exist_ok=True)
    ext = os.path.splitext(logo_url.split('?')[0])[1].lower()
    if ext not in [".png", ".jpg", ".jpeg", ".webp", ".svg"]:
        ext = ".png"
    out_path = os.path.join(assets_dir, f"{squad_id}{ext}")

    try:
        r = requests_mod.get(logo_url, timeout=30)
        if r.status_code != 200:
            return None
        # Try to load via PIL to ensure it's an image; convert to PNG and normalize size
        try:
            img = Image_mod.open(io.BytesIO(r.content)).convert("RGBA")
            # Re-save as PNG for consistent handling and standardize height
            out_path = os.path.join(assets_dir, f"{squad_id}.png")
            target_h = 20  # px (slightly smaller for denser charts)
            w, h = img.size
            if h > 0:
                scale = target_h / float(h)
                new_w = max(1, int(w * scale))
                img = img.resize((new_w, target_h))
            img.save(out_path)
        except Exception:
            # Save raw bytes as-is
            with open(out_path, 'wb') as f:
                f.write(r.content)
        return out_path
    except Exception:
        return None


def _initials(name: str) -> str:
    parts = re.split(r"\s+", name.strip())
    # Use first letter of first two words with letters
    letters = [p[0] for p in parts if p and p[0].isalpha()]
    if not letters:
        return "?"
    return (letters[0] + (letters[1] if len(letters) > 1 else "")).upper()


def plot_ranked(df: pd.DataFrame, logo_paths: Dict[int, Optional[str]], plt, OffsetImage, AnnotationBbox, Image_mod, title_prefix: str, include_logos: bool):
    # Prepare data (already filtered/sorted by caller)
    data = df.reset_index(drop=True)

    # Build labels: Player (Club)
    labels = [f"{row.playerName} ({row.squadName})" for _, row in data.iterrows()]
    values = data['openPlayXG90'].values

    # Dynamic figure height: ~0.6 per row, min 6, max 24
    height = min(max(6, 0.6 * len(data) + 2), 24)
    fig, ax = plt.subplots(figsize=(12, height))
    y_pos = range(len(data))
    bars = ax.barh(y_pos, values, color="#1f77b4")

    ax.set_yticks(list(y_pos))
    ax.set_yticklabels(labels)
    ax.invert_yaxis()
    ax.set_xlabel("Open-Play xG per 90")

    comp = str(data.get("competitionName", pd.Series([""])).iloc[0]) if "competitionName" in data.columns else ""
    season = str(data.get("season", pd.Series([""])).iloc[0]) if "season" in data.columns else ""
    title_bits = [title_prefix]
    if comp:
        title_bits.append(comp)
    if season:
        title_bits.append(str(season))
    ax.set_title(" — ".join(title_bits))

    max_val = max(values) if len(values) else 1.0
    right_pad = 0.18 if not include_logos else 0.50  # extra space only when drawing logos
    ax.set_xlim(0, max_val * (1 + right_pad))

    # Annotate bars with values (slightly right of bar end)
    for bar, val in zip(bars, values):
        ax.text(val + max_val * 0.02, bar.get_y() + bar.get_height() / 2,
                f"{val:.3f}", va='center')

    # Add logos near the right end of bars; draw initials if missing
    for idx, row in enumerate(data.itertuples(index=False)):
        sid = int(getattr(row, 'squadId'))
        logo_path = logo_paths.get(sid)
        val = float(getattr(row, 'openPlayXG90'))
        x_pos = val + (max_val * (0.02 if not include_logos else 0.10))
        if include_logos and logo_path and os.path.exists(logo_path):
            try:
                img = Image_mod.open(logo_path)
                # Already resized to 24px height upon saving; display 1:1
                imagebox = OffsetImage(img, zoom=1.0)
                ab = AnnotationBbox(imagebox, (x_pos, float(idx)), frameon=False,
                                    xycoords=('data', 'data'), box_alignment=(0.5, 0.5), zorder=5)
                ax.add_artist(ab)
            except Exception:
                pass
        elif include_logos:
            # Draw circle with initials as graceful fallback
            import matplotlib.patches as mpatches  # local to avoid hard dep at top
            circ = mpatches.Circle((float(x_pos), float(idx)), 0.045 * max_val, transform=ax.transData,
                                   color="#dddddd", zorder=4)
            ax.add_patch(circ)
            ini = _initials(str(getattr(row, 'squadName', '')))
            ax.text(float(x_pos), float(idx), ini, ha='center', va='center', fontsize=8, zorder=6)

    plt.tight_layout()
    return fig, ax


def main():
    load_env()

    iteration = int(os.getenv('IMPECT_ITERATION', '1236'))
    positions = [p.strip() for p in os.getenv('IMPECT_POSITIONS', 'CENTER_FORWARD').split(',') if p.strip()]
    include_logos = str(os.getenv('IMPECT_INCLUDE_LOGOS', '0')).strip().lower() in ("1", "true", "yes")

    token = os.getenv('IMPECT_TOKEN')
    username = os.getenv('IMPECT_USERNAME')
    password = os.getenv('IMPECT_PASSWORD')

    api = Impect()
    if token:
        api.init(token)
    else:
        if not (username and password):
            creds = get_windows_credentials()
            if creds:
                username, password, target_used = creds
                print(f"Using Windows credentials from target: {target_used}")
        if username and password:
            print(f"Attempting login as: {username}")
            token = api.login(username, password)
        else:
            print("Missing credentials. Set IMPECT_TOKEN or IMPECT_USERNAME/IMPECT_PASSWORD or store in Windows Credential Manager.")
            sys.exit(1)

    # Compute ranking
    df = getPlayerOpenPlayXG90(iteration=iteration, positions=positions, token=token)

    # Filter by minimum matchShare: >= 20% of the maximum matchShare
    if 'matchShare' in df.columns and not df['matchShare'].isna().all():
        max_ms = float(df['matchShare'].max())
        threshold = 0.2 * max_ms
        df = df[df['matchShare'] >= threshold].copy()
        print(f"Applied matchShare filter: >= {threshold:.3f} (20% of max {max_ms:.3f}). Remaining: {len(df)}")
    else:
        print("Warning: matchShare not found; skipping threshold filter.")

    # Sort and plot full list (not just Top 10)
    ranked = df.sort_values('openPlayXG90', ascending=False)
    squad_ids = sorted(set(int(s) for s in ranked['squadId'].tolist()))

    # Ensure plotting libs
    plt, OffsetImage, AnnotationBbox, Image_mod, requests_mod = ensure_libs(include_logos)

    logo_paths: Dict[int, Optional[str]] = {}
    if include_logos:
        # Fetch squad logos, then download locally for those that exist
        logo_urls = fetch_squad_logos(iteration=iteration, token=token, squad_ids=squad_ids)
        for sid, url in logo_urls.items():
            if url:
                path = save_logo(url, sid, requests_mod, Image_mod)
                logo_paths[sid] = path
            else:
                logo_paths[sid] = None

    # Plot full ranking
    title_prefix = "CF Open-Play xG/90"
    fig, ax = plot_ranked(ranked, logo_paths, plt, OffsetImage, AnnotationBbox, Image_mod, title_prefix, include_logos)

    # Save PNG
    output_dir = os.getenv('IMPECT_OUTPUT_DIR', os.path.join(REPO_ROOT, 'implementations', 'output'))
    os.makedirs(output_dir, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    pos_slug = '-'.join(positions)
    out_path = os.path.join(output_dir, f'open_play_xg90_full_iter_{iteration}_{pos_slug}_{ts}.png')
    fig.savefig(out_path, dpi=200)
    print(f"Saved chart to: {out_path}")


if __name__ == '__main__':
    main()
