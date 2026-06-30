"""
scrape.py — House PTR (Periodic Transaction Report) parser
Replaces Capitol Trades scraper which was blocked by Vercel/429.

Data source: https://disclosures-clerk.house.gov (official US government)
- No bot detection, no rate limits, no API key required
- Fetches the 2026 PTR filing index, downloads individual PDFs,
  extracts BUY transactions with ticker symbols.

Filters applied:
- Transaction type = Purchase (P) only
- Ticker must be present (1-5 uppercase letters)
- Skip bond/fund/ETF categories flagged as OT with no real ticker interest
- Trade date within last 30 days
"""

import urllib.request
import urllib.parse
import re
import io
import json
import time
import random
from datetime import datetime, timedelta
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "pdfplumber"])
    import pdfplumber

BASE_HOUSE = "https://disclosures-clerk.house.gov"
LOOKBACK_DAYS = 35  # fetch trades filed within last 35 days

SKIP_TICKERS = {"NA", "N/A", "SP", "US", "PR", "DC", "AS"}
SKIP_KW = ["treasury", "municipal", "muni", "bond fund", "t-bill",
           "government bond", "cd ", "certificate of deposit", "money market"]

OWNER_MAP = {
    "SP": "spouse", "JT": "joint", "DC": "dependent_child",
    "Self": "direct", "Joint": "joint", "Spouse": "spouse",
    "Child": "child", "Dependent Child": "child",
    "": "direct",   # empty owner = filed by member directly
}


def get_filing_list(year="2026"):
    """POST to House clerk search to get all PTR filings for the year."""
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
    no_proxy = urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(no_proxy)

    with opener.open(req, timeout=20) as r:
        html = r.read().decode("utf-8", errors="replace")

    # Extract: (year, filing_id, member_name)
    rows = re.findall(
        r'href="public_disc/ptr-pdfs/(\d{4})/(\d+)\.pdf"[^>]*>\s*([^<]+)</a>',
        html
    )
    print(f"Found {len(rows)} PTR filings for {year}")
    return rows


def fetch_pdf(filing_id, year="2026"):
    url = f"{BASE_HOUSE}/public_disc/ptr-pdfs/{year}/{filing_id}.pdf"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; congress-ptr-parser/2.0)"})
    no_proxy = urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(no_proxy)
    with opener.open(req, timeout=15) as r:
        return r.read()


def parse_pdf(pdf_bytes, member_name, filing_id, cutoff_date):
    """
    Parse a House PTR PDF and return a list of BUY trade dicts.
    
    PTR text format (key lines):
      JT  Boeing Company (BA) [ST]  P  02/07/2025  05/29/2026  $15,001 - $50,000
      SP  Apple Inc. - Common Stock (AAPL) [ST]  P  06/01/2026  06/10/2026  $1,001 - $15,000
    """
    trades = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as e:
        print(f"  PDF parse error ({filing_id}): {e}")
        return trades

    # Detect party (sometimes present in text)
    party_match = re.search(r"\b(Republican|Democrat|Independent)\b", full_text, re.IGNORECASE)
    party = party_match.group(1).capitalize() if party_match else "Unknown"

    # State/District
    sd_match = re.search(r"State/District:\s*([A-Z]{2}\d{2})", full_text)
    state_district = sd_match.group(1) if sd_match else ""
    state = state_district[:2] if state_district else ""

    # ---------------------------------------------------------------
    # Primary pattern: captures owner prefix, asset name, ticker,
    # asset type, transaction type, traded date, notif date, amount
    # ---------------------------------------------------------------
    # Handles lines like:
    #   JT  Apple Inc. - Common Stock (AAPL) [ST]  P  06/01/2026  06/10/2026  $1,001 - $15,000
    #   SP  NVIDIA Corp (NVDA) [ST] P 05/15/2026 05/20/2026 $50,001 - $100,000
    pattern = re.compile(
        r"(?:^|\n)"
        r"(JT|SP|DC|Self|Joint|Spouse|Child|Dependent Child)?\s*"  # owner (optional)
        r"([A-Za-z][A-Za-z0-9\s\.,\-&']+?)"                        # asset name
        r"\(([A-Z]{1,5})\)"                                          # (TICKER)
        r"\s*\[(ST|OT|PT)\]"                                         # [asset type]
        r"\s+(P)\b"                                                   # P = Purchase
        r"[^\n]*?(\d{2}/\d{2}/\d{4})"                               # traded date
        r"[^\n]*?(\d{2}/\d{2}/\d{4})"                               # notification date
        r"[^\n]*?(\$[\d,]+\s*[-–]\s*\$[\d,]+)",                     # amount range
        re.MULTILINE
    )

    for m in pattern.finditer(full_text):
        owner_raw, issuer, ticker, asset_type, tx_type, traded_str, notif_str, amount = m.groups()

        # Clean up
        ticker  = ticker.strip().upper()
        issuer  = re.sub(r"\s+", " ", issuer).strip().rstrip(" -,.")
        amount  = re.sub(r"\s+", " ", amount).strip()
        owner_raw = (owner_raw or "").strip()

        # Skip invalid tickers
        if ticker in SKIP_TICKERS or len(ticker) < 1:
            continue
        if any(kw in issuer.lower() for kw in SKIP_KW):
            continue

        # Parse traded date
        try:
            tx_dt = datetime.strptime(traded_str, "%m/%d/%Y")
        except ValueError:
            continue

        # Only include if within lookback window
        if tx_dt < cutoff_date:
            continue

        trade_age_days = (datetime.now() - tx_dt).days

        trades.append({
            "politician":     member_name.strip(),
            "chamber":        "House",
            "party":          party,
            "state":          state,
            "issuer":         issuer,
            "ticker":         ticker,
            "asset_type":     asset_type,
            "traded_date":    traded_str,
            "trade_age_days": trade_age_days,
            "notification_date": notif_str,
            "owner":          owner_raw,
            "owner_type":     OWNER_MAP.get(owner_raw, "direct"),  # default=direct (filer's own trade)
            "type":           "buy",
            "size":           amount,
            "filing_id":      filing_id,
            "source":         "house_ptr",
        })

    return trades


def run_scraper(year="2026", max_pdfs=None, delay_range=(0.5, 1.5)):
    """
    Main entry point. Fetches filing list, downloads PDFs filed
    within the last LOOKBACK_DAYS days, parses BUY trades.
    """
    cutoff_date = datetime.now() - timedelta(days=LOOKBACK_DAYS)
    print(f"Cutoff date: {cutoff_date.strftime('%Y-%m-%d')} (last {LOOKBACK_DAYS} days)")

    # Get all filings for this year
    filings = get_filing_list(year)
    if not filings:
        print("ERROR: No filings found")
        return []

    # We can't know the filing date from the index alone.
    # Strategy: process all filings and filter by traded_date in parse step.
    # To keep runtime reasonable, limit to last N filings (sorted by filing_id desc = most recent first).
    # Filing IDs are monotonically increasing, so reverse order = newest first.
    filings_sorted = sorted(filings, key=lambda x: int(x[1]), reverse=True)

    if max_pdfs:
        filings_sorted = filings_sorted[:max_pdfs]
        print(f"Processing last {max_pdfs} filings (most recent)")
    else:
        print(f"Processing all {len(filings_sorted)} filings")

    all_trades = []
    processed = 0
    errors = 0

    for i, (yr, fid, name) in enumerate(filings_sorted):
        try:
            pdf_bytes = fetch_pdf(fid, yr)
            trades = parse_pdf(pdf_bytes, name, fid, cutoff_date)
            if trades:
                print(f"  [{i+1}] {name.strip()} ({fid}): {len(trades)} BUY trade(s)")
                for t in trades:
                    print(f"       {t['ticker']:6} {t['traded_date']}  {t['size']}")
                all_trades.extend(trades)
            processed += 1
        except Exception as e:
            errors += 1
            print(f"  [{i+1}] {name.strip()} ({fid}): ERROR — {e}")

        # Polite delay
        if i < len(filings_sorted) - 1:
            time.sleep(random.uniform(*delay_range))

    print(f"\nProcessed: {processed} PDFs | Errors: {errors}")
    print(f"Total BUY trades (last {LOOKBACK_DAYS}d): {len(all_trades)}")
    return all_trades


if __name__ == "__main__":
    print(f"Starting House PTR scrape at {datetime.utcnow().isoformat()}Z")

    # In GitHub Actions: process last 80 filings (covers ~30 days of activity)
    # Adjust max_pdfs based on how many members typically file per month
    trades = run_scraper(year="2026", max_pdfs=80, delay_range=(0.3, 0.8))

    output = {
        "scraped_at":   datetime.utcnow().isoformat() + "Z",
        "source":       "house_ptr_official",
        "trade_count":  len(trades),
        "lookback_days": LOOKBACK_DAYS,
        "trades":       trades,
    }

    Path("trades.json").write_text(json.dumps(output, indent=2))
    print(f"Saved trades.json ({len(trades)} trades)")
