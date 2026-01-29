import os, re, smtplib, ssl, sys
import feedparser
import requests
import pandas as pd
from bs4 import BeautifulSoup
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone
import yaml

def load_yaml(p):
    with open(p, "r") as f:
        return yaml.safe_load(f)

def fetch_rss(url):
    d = feedparser.parse(url)
    items = []
    for e in d.entries:
        items.append({
            "title": getattr(e, "title", "").strip(),
            "url": getattr(e, "link", "").strip(),
            "summary": getattr(e, "summary", "")[:500].strip() if hasattr(e, "summary") else "",
            "source": url,
            "type": "rss"
        })
    return items

def fetch_html_list(url, item_selector, title_attr="text", href_attr="href"):
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1"
    }
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    items = []
    for a in soup.select(item_selector):
        href = a.get(href_attr) if href_attr != "text" else a.get_text(strip=True)
        title = a.get_text(strip=True) if title_attr == "text" else (a.get(title_attr) or "").strip()
        if not href:
            continue
        # Normalize relative URLs
        if href.startswith("/"):
            from urllib.parse import urljoin
            href = urljoin(url, href)
        items.append({
            "title": title or "(no title)",
            "url": href,
            "summary": "",
            "source": url,
            "type": "html"
        })
    return items

def keyword_match(text, keywords):
    text_l = text.lower()
    return any(k.lower() in text_l for k in keywords)

def keyword_excluded(text, excludes):
    text_l = text.lower()
    return any(x.lower() in text_l for x in excludes)

def extract_price(text):
    """Extract price from text, returns None if not found"""
    import re
    # First try to find prices with dollar signs (more reliable)
    price_pattern_with_dollar = r'\$(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)'
    matches = re.findall(price_pattern_with_dollar, text)
    if matches:
        try:
            # Take the first match with $, remove commas
            price_str = matches[0].replace(',', '')
            price = float(price_str)
            # Only consider realistic rent prices (between $500 and $10,000)
            if 500 <= price <= 10000:
                return price
        except (ValueError, IndexError):
            pass

    # Fallback: look for standalone numbers that look like rent
    # Must be 4 digits (1000-9999) to avoid street numbers like "80s"
    price_pattern_no_dollar = r'\b(\d{4})\b'
    matches = re.findall(price_pattern_no_dollar, text)
    if matches:
        try:
            price = float(matches[0])
            if 500 <= price <= 10000:
                return price
        except (ValueError, IndexError):
            pass

    return None

def extract_street_number(text):
    """Extract street number from address-like text"""
    import re
    # Look for patterns like "90 Washington Street" or "123 East 86th"
    street_pattern = r'(\d{1,4})\s+(?:East|West|North|South)?\s*\d{1,3}(?:st|nd|rd|th)?'
    match = re.search(street_pattern, text, re.IGNORECASE)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None

def apply_hard_filters(item, hard_reqs):
    """Apply hard requirement filters, returns True if item passes"""
    if not hard_reqs:
        return True

    text = f"{item.get('title','')} {item.get('summary','')} {item.get('url','')}".lower()

    # Max rent check
    if 'max_rent' in hard_reqs:
        price = extract_price(text)
        if price and price > hard_reqs['max_rent']:
            return False

    # Studio only check
    if hard_reqs.get('studio_only'):
        # Check if it mentions bedroom counts that aren't studio
        if re.search(r'\b(1|2|3|4)\s*(br|bed|bedroom)', text, re.IGNORECASE):
            # If it's not explicitly called a studio, exclude it
            if 'studio' not in text:
                return False

    # Laundry requirement
    if hard_reqs.get('require_laundry'):
        laundry_keywords = ['laundry', 'washer', 'dryer', 'w/d', 'in-unit']
        if not any(kw in text for kw in laundry_keywords):
            return False

    # Exclude neighborhoods
    if 'exclude_neighborhoods' in hard_reqs:
        for neighborhood in hard_reqs['exclude_neighborhoods']:
            if neighborhood.lower() in text:
                return False

    # Exclude above street number
    if 'exclude_above_street' in hard_reqs:
        street_num = extract_street_number(text)
        if street_num and street_num > hard_reqs['exclude_above_street']:
            return False

    return True

def score_preferences(item, prefs):
    """Score item based on preferences, higher is better"""
    if not prefs:
        return 0

    score = 0
    text = f"{item.get('title','')} {item.get('summary','')} {item.get('url','')}".lower()

    if prefs.get('furnished') and 'furnished' in text:
        score += 2

    if prefs.get('sublet_or_short_term'):
        sublet_keywords = ['sublet', 'sublease', 'short-term', 'short term', 'temporary']
        if any(kw in text for kw in sublet_keywords):
            score += 2

    return score

def dedupe(items):
    seen = set()
    out = []
    for it in items:
        key = it["url"].strip().lower()
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out

def send_email(cfg, matches):
    from_addr = os.environ.get(cfg["email"]["from_env"], "")
    to_addr = os.environ.get(cfg["email"]["to_env"], "")
    smtp_host = os.environ.get(cfg["email"]["smtp_host_env"], "")
    smtp_port = int(os.environ.get(cfg["email"]["smtp_port_env"], "587"))
    smtp_user = os.environ.get(cfg["email"]["smtp_user_env"], "")
    smtp_pass = os.environ.get(cfg["email"]["smtp_pass_env"], "")

    if not (from_addr and to_addr and smtp_host and smtp_user and smtp_pass):
        print("⚠️ Missing SMTP env vars; skipping email.")
        return

    subject = f"{cfg['email'].get('subject_prefix','[Apartment Scout]')} {len(matches)} match(es) – {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
    html_rows = []
    for m in matches:
        title = (m['title'] or '').replace('\n',' ').strip()
        url = m['url']
        src = m['source']
        html_rows.append(f"<li><a href='{url}'>{title}</a> <small>({src})</small></li>")
    html_body = f"""
    <html><body>
    <h2>{subject}</h2>
    <p>Keywords: {', '.join(cfg['filters']['keywords'])}</p>
    <ol>
      {''.join(html_rows) if html_rows else '<li>No matches today.</li>'}
    </ol>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls(context=context)
            server.login(smtp_user, smtp_pass)
            server.sendmail(from_addr, [to_addr], msg.as_string())
        print(f"📧 Sent email to {to_addr} with {len(matches)} match(es).")
    except Exception as e:
        print(f"❌ Failed to send email: {e}")

def main():
    cfg = load_yaml("config/config.yaml")
    os.makedirs(cfg["app"]["output_dir"], exist_ok=True)

    all_items = []

    # RSS sources
    for src in cfg["sources"].get("rss", []):
        try:
            items = fetch_rss(src["url"])
            for it in items:
                it["feed_name"] = src.get("name", "rss")
            all_items.extend(items)
        except Exception as e:
            print(f"RSS error for {src['url']}: {e}")

    # HTML sources
    for src in cfg["sources"].get("html", []):
        try:
            items = fetch_html_list(
                src["url"],
                src["item_selector"],
                src.get("title_attr", "text"),
                src.get("href_attr", "href"),
            )
            for it in items:
                it["feed_name"] = src.get("name", "html")
            all_items.extend(items)
        except Exception as e:
            print(f"HTML error for {src['url']}: {e}")

    all_items = dedupe(all_items)

    # Filter by keywords/excludes over title+summary+url
    kws   = cfg["filters"].get("keywords", [])
    excl  = cfg["filters"].get("exclude", [])
    hard_reqs = cfg["filters"].get("hard_requirements", {})
    prefs = cfg["filters"].get("preferences", {})

    matches = []
    for it in all_items:
        hay = f"{it.get('title','')} {it.get('summary','')} {it.get('url','')}"

        # Apply keyword and exclusion filters
        if not keyword_match(hay, kws):
            continue
        if keyword_excluded(hay, excl):
            continue

        # Apply hard requirement filters
        if not apply_hard_filters(it, hard_reqs):
            continue

        # Calculate preference score
        it['preference_score'] = score_preferences(it, prefs)
        matches.append(it)

    # Sort by preference score (highest first), then by title/url for stability
    matches.sort(key=lambda x: (-x.get("preference_score", 0), x.get("type",""), x.get("title","").lower(), x.get("url","").lower()))

    # Write outputs
    df = pd.DataFrame(matches)
    csv_path = cfg["output_files"]["csv"]
    md_path  = cfg["output_files"]["markdown_summary"]
    df.to_csv(csv_path, index=False)

    with open(md_path, "w") as f:
        f.write(f"# Apartment Scout Matches\n\nGenerated: {datetime.utcnow().isoformat()}Z\n\n")
        if matches:
            for m in matches:
                f.write(f"- [{m['title']}]({m['url']})  \n")
        else:
            f.write("_No matches._\n")

    print(f"✅ Wrote {len(matches)} matches to {csv_path} and {md_path}")

    # Email
    if matches or cfg["email"].get("send_if_zero", False):
        try:
            send_email(cfg, matches)
        except Exception as e:
            print(f"❌ Email error: {e}")

if __name__ == "__main__":
    main()
