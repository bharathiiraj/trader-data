"""
scrape.py — Capitol Trades data relay scraper
Runs in a PUBLIC GitHub repo (bharathiiraj/trader-data)
No network restrictions — can reach Capitol Trades freely
Saves trades.json which trader-bot reads via raw.githubusercontent.com
"""
import requests, json, re, time, random
from datetime import datetime
from pathlib import Path
from bs4 import BeautifulSoup

AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

OMAP    = {"Self":"direct","Joint":"direct",
           "Undisclosed":"managed","Spouse":"spouse","Child":"child"}
SKIP_KW = ["treasury","municipal","muni","bond fund","t-bill","government bond"]

def scrape(max_pages=15):
    base = "https://www.capitoltrades.com"
    url  = f"{base}/trades?txDate=30d&txType=buy"

    session = requests.Session()
    session.headers.update({
        "User-Agent":                random.choice(AGENTS),
        "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language":           "en-US,en;q=0.9",
        "Accept-Encoding":           "gzip, deflate, br",
        "Connection":                "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest":            "document",
        "Sec-Fetch-Mode":            "navigate",
        "Sec-Fetch-Site":            "none",
        "Sec-Fetch-User":            "?1",
    })

    # Warm up session with homepage
    try:
        session.get(base, timeout=10)
        time.sleep(random.uniform(2.0, 4.0))
    except Exception as e:
        print(f"Homepage warm-up failed: {e}")

    trades = []
    total  = None

    for page in range(1, max_pages + 1):
        page_url = url + (f"&page={page}" if page > 1 else "")
        if page > 1:
            session.headers["Referer"] = url

        success = False
        for attempt in range(3):
            try:
                r = session.get(page_url, timeout=20)
                if r.status_code == 200:
                    success = True
                    break
                elif r.status_code == 429:
                    wait = max(int(r.headers.get("Retry-After", 45)), 45) * (attempt + 1)
                    print(f"  429 (attempt {attempt+1}) — waiting {wait}s...")
                    time.sleep(wait)
                    session.headers["User-Agent"] = random.choice(AGENTS)
                else:
                    print(f"  HTTP {r.status_code} — stopping")
                    return trades
            except Exception as e:
                print(f"  Error: {e}")
                time.sleep(5 * (attempt + 1))

        if not success:
            print(f"Page {page} failed after 3 attempts — stopping")
            break

        soup = BeautifulSoup(r.text, "html.parser")

        if total is None:
            for tag in soup.find_all(string=True):
                m = re.search(r"Page\s+(\d+)\s+of\s+(\d+)", tag)
                if m:
                    total = int(m.group(2))
                    print(f"Total pages: {total}")
                    break
            if total is None:
                total = max_pages

        tbl = soup.find("table")
        if not tbl:
            break

        cnt = 0
        for row in tbl.find_all("tr")[1:]:
            cells = row.find_all("td")
            if len(cells) < 8:
                continue
            try:
                pt      = cells[0].get_text(separator=" ", strip=True)
                pl      = cells[0].find("a")
                pn      = pl.get_text(strip=True) if pl else ""
                party   = ("Republican" if "Republican" in pt else
                           "Democrat"   if "Democrat"   in pt else "Unknown")
                chamber = ("House"  if "House"  in pt else
                           "Senate" if "Senate" in pt else "Unknown")
                il      = cells[1].find("a")
                iname   = il.get_text(strip=True) if il else ""
                ticker  = ""
                for s in cells[1].find_all(string=True):
                    m2 = re.search(r"([A-Z]{1,5}):US", s)
                    if m2:
                        ticker = m2.group(1)
                        break
                tx = cells[6].get_text(strip=True).lower()
                if "buy" not in tx:
                    continue
                if any(k in iname.lower() for k in SKIP_KW):
                    continue
                if not ticker:
                    continue
                owner      = cells[5].get_text(strip=True)
                traded_str = cells[3].get_text(strip=True)
                trade_age  = None
                for fmt in ("%b %d, %Y", "%Y-%m-%d", "%d %b %Y", "%B %d, %Y"):
                    try:
                        trade_age = (datetime.now() - datetime.strptime(traded_str, fmt)).days
                        break
                    except ValueError:
                        continue
                trades.append({
                    "politician":     pn,
                    "chamber":        chamber,
                    "party":          party,
                    "issuer":         iname,
                    "ticker":         ticker,
                    "published":      cells[2].get_text(strip=True),
                    "traded_date":    traded_str,
                    "trade_age_days": trade_age,
                    "filed_after":    cells[4].get_text(strip=True).replace("days","").strip(),
                    "owner":          owner,
                    "owner_type":     OMAP.get(owner, "unknown"),
                    "type":           tx,
                    "size":           cells[7].get_text(strip=True),
                    "price":          cells[8].get_text(strip=True) if len(cells) > 8 else "N/A",
                })
                cnt += 1
            except Exception:
                continue

        print(f"Page {page}/{total}: {cnt} trades")
        if page >= total:
            break
        time.sleep(random.uniform(2.5, 5.0))

    return trades


if __name__ == "__main__":
    print(f"Starting scrape at {datetime.utcnow().isoformat()}Z")
    trades = scrape()
    print(f"Total BUY trades: {len(trades)}")

    output = {
        "scraped_at": datetime.utcnow().isoformat() + "Z",
        "trade_count": len(trades),
        "trades": trades,
    }

    Path("trades.json").write_text(json.dumps(output, indent=2))
    print(f"Saved trades.json ({len(trades)} trades)")
