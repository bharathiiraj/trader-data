"""
scrape.py — House PTR (Periodic Transaction Report) parser
Incremental mode: only downloads PDFs newer than last run checkpoint.
Rolling 30-day archive: merges new trades into trades.json, evicts trades > 30 days old.

Data source: https://disclosures-clerk.house.gov (official US government)
- No bot detection, no rate limits, no API key required

Two GitHub files maintained:
  trades.json       — rolling 30-day BUY trade archive (read by Daily Duty + Pulse Check)
  ptr_checkpoint.json — last seen filing_id + scrape metadata (read by next run)

Flow:
  1. Fetch filing index → get all filing_ids for 2026
  2. Load ptr_checkpoint.json from GitHub → get last_seen_id
  3. Filter to only filing_ids > last_seen_id  (new PDFs only)
  4. Download + parse new PDFs
  5. Load existing trades.json from GitHub
  6. Merge new trades + evict trades older than 30 days
  7. Push updated trades.json + ptr_checkpoint.json back to GitHub
"""

import urllib.request
import urllib.parse
import re
import io
import json
import time
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "pdfplumber"])
    import pdfplumber

BASE_HOUSE      = "https://disclosures-clerk.house.gov"
GITHUB_REPO     = "bharathiiraj/trader-data"
ARCHIVE_DAYS    = 30    # keep trades up to 30 days old in trades.json
INITIAL_LOOKBACK = 35  # first-ever run: look back 35 days (no checkpoint yet)

SKIP_TICKERS = {"NA", "N/A", "SP", "US", "PR", "DC", "AS"}
SKIP_KW = ["treasury", "municipal", "muni", "bond fund", "t-bill",
           "government bond", "cd ", "certificate of deposit", "money market"]

OWNER_MAP = {
    "SP": "spouse", "JT": "joint", "DC": "dependent_child",
    "Self": "direct", "Joint": "joint", "Spouse": "spouse",
    "Child": "child", "Dependent Child": "child",
    "": "direct",   # empty owner = filed by member directly
}


# ── GitHub helpers ────────────────────────────────────────────────────────────

def github_get(path):
    """GET a file from GitHub. Returns (content_str, sha) or (None, None)."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    try:
        import requests
        r = requests.get(url, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "trader-bot/2.0"
        }, timeout=15, verify=False)
        if r.status_code == 404:
            return None, None
        r.raise_for_status()
        data = r.json()
        import base64
        content = base64.b64decode(data["content"]).decode("utf-8")
        return content, data["sha"]
    except Exception as e:
        print(f"  [WARN] GitHub GET {path}: {e}")
        return None, None


def github_put(path, content_str, sha, commit_msg):
    """PUT (create or update) a file on GitHub."""
    import requests, base64
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    body = {
        "message": commit_msg,
        "content": base64.b64encode(content_str.encode()).decode(),
    }
    if sha:
        body["sha"] = sha
    r = requests.put(url, json=body, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "trader-bot/2.0"
    }, timeout=20, verify=False)
    if r.status_code in (200, 201):
        return r.json()["content"]["sha"]
    else:
        raise RuntimeError(f"GitHub PUT {path} failed {r.status_code}: {r.text[:200]}")


# ── House clerk helpers ───────────────────────────────────────────────────────

def get_filing_list(year="2026"):
    """POST to House clerk search → returns list of (year, filing_id, member_name)."""
    hdrs = {
        "User-Agent": "Mozilla/5.0 (compatible; congress-ptr-parser/2.0)",
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": f"{BASE_HOUSE}/FinancialDisclosure",
    }
    form_data = urllib.parse.urlencode({
        "LastName": "", "State": "", "District": "",
        "FilingYear": year, "SearchYear": year, "Search": "Search"
    }).encode()

    req = urllib.request.Request(
        f"{BASE_HOUSE}/FinancialDisclosure/ViewMemberSearchResult",
        data=form_data, headers=hdrs, method="POST"
    )
    no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with no_proxy.open(req, timeout=20) as r:
        html = r.read().decode("utf-8", errors="replace")

    rows = re.findall(
        r'href="public_disc/ptr-pdfs/(\d{4})/(\d+)\.pdf"[^>]*>\s*([^<]+)</a>',
        html
    )
    print(f"  Filing index: {len(rows)} total PTRs for {year}")
    return rows


def fetch_pdf(filing_id, year="2026"):
    url = f"{BASE_HOUSE}/public_disc/ptr-pdfs/{year}/{filing_id}.pdf"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; congress-ptr-parser/2.0)"
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=15) as r:
        return r.read()


def parse_pdf(pdf_bytes, member_name, filing_id, cutoff_date):
    """Parse a PTR PDF and return list of BUY trade dicts."""
    trades = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as e:
        print(f"    PDF parse error ({filing_id}): {e}")
        return trades

    party_match = re.search(r"\b(Republican|Democrat|Independent)\b", full_text, re.IGNORECASE)
    party = party_match.group(1).capitalize() if party_match else "Unknown"

    sd_match = re.search(r"State/District:\s*([A-Z]{2}\d{2})", full_text)
    state_district = sd_match.group(1) if sd_match else ""
    state = state_district[:2] if state_district else ""

    pattern = re.compile(
        r"(?:^|\n)"
        r"(JT|SP|DC|Self|Joint|Spouse|Child|Dependent Child)?\s*"
        r"([A-Za-z][A-Za-z0-9\s\.,\-&']+?)"
        r"\(([A-Z]{1,5})\)"
        r"\s*\[(ST|OT|PT)\]"
        r"\s+(P)\b"
        r"[^\n]*?(\d{2}/\d{2}/\d{4})"
        r"[^\n]*?(\d{2}/\d{2}/\d{4})"
        r"[^\n]*?(\$[\d,]+\s*[-–]\s*\$[\d,]+)",
        re.MULTILINE
    )

    for m in pattern.finditer(full_text):
        owner_raw, issuer, ticker, asset_type, tx_type, traded_str, notif_str, amount = m.groups()

        ticker    = ticker.strip().upper()
        issuer    = re.sub(r"\s+", " ", issuer).strip().rstrip(" -,.")
        amount    = re.sub(r"\s+", " ", amount).strip()
        owner_raw = (owner_raw or "").strip()

        if ticker in SKIP_TICKERS or len(ticker) < 1:
            continue
        if any(kw in issuer.lower() for kw in SKIP_KW):
            continue

        try:
            tx_dt = datetime.strptime(traded_str, "%m/%d/%Y")
        except ValueError:
            continue

        if tx_dt < cutoff_date:
            continue

        trade_age_days = (datetime.now() - tx_dt).days

        trades.append({
            "politician":        member_name.strip(),
            "chamber":           "House",
            "party":             party,
            "state":             state,
            "issuer":            issuer,
            "ticker":            ticker,
            "asset_type":        asset_type,
            "traded_date":       traded_str,
            "trade_age_days":    trade_age_days,
            "notification_date": notif_str,
            "owner":             owner_raw,
            "owner_type":        OWNER_MAP.get(owner_raw, "direct"),
            "type":              "buy",
            "size":              amount,
            "filing_id":         filing_id,
            "source":            "house_ptr",
        })

    return trades


# ── Main scraper ──────────────────────────────────────────────────────────────

def run_scraper(year="2026", dry_run=False, delay_range=(0.3, 0.8)):
    """
    Incremental scraper:
    - Only downloads PDFs with filing_id > last checkpoint
    - Merges into rolling 30-day archive in trades.json
    - Saves new checkpoint to ptr_checkpoint.json
    """
    now_utc = datetime.now(timezone.utc)
    ts      = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"\n{'='*60}")
    print(f"TRADER-BOT PTR Scraper — {ts}")
    print(f"Mode: {'DRY RUN (no GitHub push)' if dry_run else 'LIVE'}")
    print(f"{'='*60}\n")

    # ── 1. Get full filing index ──────────────────────────────────────────────
    print("STEP 1 — Fetching filing index...")
    all_filings = get_filing_list(year)
    if not all_filings:
        print("ERROR: empty filing index")
        return 0, 0

    # Sort newest-first by filing_id (monotonically increasing integer)
    all_filings_sorted = sorted(all_filings, key=lambda x: int(x[1]), reverse=True)
    max_id_in_index    = int(all_filings_sorted[0][1])

    # ── 2. Load checkpoint ────────────────────────────────────────────────────
    print("\nSTEP 2 — Loading checkpoint from GitHub...")
    chk_raw, chk_sha = github_get("ptr_checkpoint.json")

    if chk_raw:
        chk = json.loads(chk_raw)
        last_seen_id = int(chk.get("last_seen_id", 0))
        print(f"  Last run: {chk.get('scraped_at','?')} | last_seen_id={last_seen_id}")
    else:
        last_seen_id = 0
        chk_sha      = None
        print(f"  No checkpoint found — first run, will process last {INITIAL_LOOKBACK} days")

    # ── 3. Filter to only new filings ────────────────────────────────────────
    print("\nSTEP 3 — Filtering to new PDFs only...")
    if last_seen_id == 0:
        # First run: use initial lookback window by date (via cutoff inside parse_pdf)
        # Still cap to last 80 to avoid a very long first run
        new_filings = all_filings_sorted[:80]
        cutoff_date = datetime.now() - timedelta(days=INITIAL_LOOKBACK)
        print(f"  First run: processing last 80 filings | cutoff={cutoff_date.date()}")
    else:
        # Incremental: only new IDs
        new_filings = [f for f in all_filings_sorted if int(f[1]) > last_seen_id]
        cutoff_date = datetime.now() - timedelta(days=ARCHIVE_DAYS)
        print(f"  Incremental: {len(new_filings)} new PDFs since id={last_seen_id}")
        if not new_filings:
            print("  Nothing new since last run.")
            # Still push an updated trades.json to refresh trade_age_days
            # (ages increment daily even with no new filings)
            pass

    # ── 4. Download + parse new PDFs ─────────────────────────────────────────
    print(f"\nSTEP 4 — Parsing {len(new_filings)} new PDF(s)...")
    new_trades  = []
    processed   = 0
    errors      = 0

    for i, (yr, fid, name) in enumerate(new_filings):
        try:
            pdf_bytes = fetch_pdf(fid, yr)
            trades    = parse_pdf(pdf_bytes, name, fid, cutoff_date)
            if trades:
                print(f"  [{i+1}/{len(new_filings)}] {name.strip()} ({fid}): {len(trades)} BUY")
                for t in trades:
                    print(f"    → {t['ticker']:6} {t['traded_date']}  {t['size']}")
                new_trades.extend(trades)
            else:
                print(f"  [{i+1}/{len(new_filings)}] {name.strip()} ({fid}): no buys")
            processed += 1
        except Exception as e:
            errors += 1
            print(f"  [{i+1}/{len(new_filings)}] ERROR {fid}: {e}")

        if i < len(new_filings) - 1:
            time.sleep(random.uniform(*delay_range))

    print(f"\n  Parsed: {processed} PDFs | Errors: {errors} | New trades: {len(new_trades)}")

    # ── 5. Load existing archive ──────────────────────────────────────────────
    print("\nSTEP 5 — Loading existing trades.json archive...")
    existing_raw, trades_sha = github_get("trades.json")

    if existing_raw:
        existing = json.loads(existing_raw)
        old_trades = existing.get("trades", [])
        print(f"  Existing archive: {len(old_trades)} trades")
    else:
        old_trades   = []
        trades_sha   = None
        print(f"  No existing archive — starting fresh")

    # ── 6. Merge + evict stale trades ────────────────────────────────────────
    print("\nSTEP 6 — Merging + evicting trades older than 30 days...")

    # Deduplicate by (politician, ticker, traded_date, filing_id)
    def trade_key(t):
        return (t["politician"], t["ticker"], t["traded_date"], t.get("filing_id",""))

    seen_keys   = {trade_key(t) for t in new_trades}
    merged      = list(new_trades)  # new trades first

    for t in old_trades:
        if trade_key(t) not in seen_keys:
            merged.append(t)
            seen_keys.add(trade_key(t))

    # Refresh trade_age_days on ALL trades (ages increment each day)
    today_str = datetime.now().strftime("%Y-%m-%d")
    active = []
    evicted = 0
    for t in merged:
        try:
            tx_dt = datetime.strptime(t["traded_date"], "%m/%d/%Y")
            age   = (datetime.now() - tx_dt).days
            if age > ARCHIVE_DAYS:
                evicted += 1
                continue
            t["trade_age_days"] = age
            active.append(t)
        except Exception:
            active.append(t)  # keep if unparseable

    print(f"  After merge: {len(active)} active trades | {evicted} evicted (>30d)")

    # ── 7. Build output + push ────────────────────────────────────────────────
    output = {
        "scraped_at":    ts,
        "source":        "house_ptr_official",
        "trade_count":   len(active),
        "lookback_days": ARCHIVE_DAYS,
        "new_this_run":  len(new_trades),
        "pdfs_scanned":  processed,
        "trades":        active,
    }

    checkpoint = {
        "scraped_at":    ts,
        "last_seen_id":  max_id_in_index,
        "total_filings": len(all_filings),
        "new_pdfs":      len(new_filings),
        "new_trades":    len(new_trades),
    }

    # Save locally always
    Path("trades.json").write_text(json.dumps(output, indent=2))
    Path("ptr_checkpoint.json").write_text(json.dumps(checkpoint, indent=2))
    print(f"\n  Saved locally: trades.json ({len(active)} trades) | ptr_checkpoint.json")

    if dry_run:
        print("\n⚠️  DRY RUN — skipping GitHub push")
        print(f"  Would push trades.json ({len(active)} trades) and ptr_checkpoint.json")
        return len(active), processed

    # Push to GitHub
    print("\nSTEP 7 — Pushing to GitHub...")
    try:
        new_trades_sha = github_put(
            "trades.json",
            json.dumps(output, indent=2),
            trades_sha,
            f"Update trades.json — {len(active)} active, {len(new_trades)} new | {ts}"
        )
        print(f"  ✅ trades.json pushed ({new_trades_sha[:12]})")
    except Exception as e:
        print(f"  ❌ trades.json push failed: {e}")

    try:
        new_chk_sha = github_put(
            "ptr_checkpoint.json",
            json.dumps(checkpoint, indent=2),
            chk_sha,
            f"Update ptr_checkpoint.json — last_id={max_id_in_index} | {ts}"
        )
        print(f"  ✅ ptr_checkpoint.json pushed ({new_chk_sha[:12]})")
    except Exception as e:
        print(f"  ❌ ptr_checkpoint.json push failed: {e}")

    print(f"\n{'='*60}")
    print(f"DONE | {len(new_trades)} new trades | {len(active)} in 30d archive | {processed} PDFs scanned")
    print(f"{'='*60}\n")

    return len(active), processed


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    dry_run = "--dry-run" in sys.argv
    total, pdfs = run_scraper(year="2026", dry_run=dry_run)
