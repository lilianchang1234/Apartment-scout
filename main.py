import yaml
import os
import pandas as pd
from dotenv import load_dotenv
from rich import print
from datetime import datetime

def load_config(path):
    with open(path, "r") as f:
        return yaml.safe_load(f)

def passes_filters(listing, rules):
    """Apply basic NYC rules."""
    rent = listing.get("price", 0)
    beds = str(listing.get("bedrooms", "")).lower()
    neighborhood = listing.get("neighborhood", "").lower()
    laundry = listing.get("amenities.laundry", False)
    street = listing.get("street", "")
    
    # hard rules
    if rent > rules["hard"]["max_rent"]:
        return False
    if rules["hard"]["studio_only"] and beds not in ["0", "studio"]:
        return False
    if rules["hard"]["require_laundry"] and not laundry:
        return False
    for bad in rules["hard"]["exclude_neighborhoods"]:
        if bad.lower() in neighborhood:
            return False
    if "street" in listing and listing["street"]:
        try:
            num = int(''.join([c for c in listing["street"] if c.isdigit()]))
            if num > rules["hard"]["exclude_above_street"]:
                return False
        except:
            pass
    return True

def main():
    load_dotenv()
    cfg = load_config("config/config.yaml")
    rules = load_config(cfg["filters"]["rules_file"])

    print("[bold cyan]Apartment Scout running...[/bold cyan]")

    # Example dummy data (replace with your real scraping logic)
    listings = [
        {"price": 2400, "bedrooms": "studio", "neighborhood": "East Village", "amenities.laundry": True, "street": "5th Ave"},
        {"price": 2600, "bedrooms": "studio", "neighborhood": "FiDi", "amenities.laundry": True, "street": "50 Wall St"},
        {"price": 2000, "bedrooms": "1", "neighborhood": "Upper West Side", "amenities.laundry": True, "street": "96th St"},
    ]

    filtered = [l for l in listings if passes_filters(l, rules)]
    df = pd.DataFrame(filtered)

    os.makedirs(cfg["app"]["output_dir"], exist_ok=True)
    csv_path = os.path.join(cfg["app"]["output_dir"], "listings.csv")
    md_path = os.path.join(cfg["app"]["output_dir"], "summary.md")

    df.to_csv(csv_path, index=False)
    with open(md_path, "w") as f:
        f.write(f"# Apartment Scout Report\n\nGenerated {datetime.now()}\n\n")
        f.write(df.to_markdown(index=False))

    print(f"[green]✅ Done![/green] Wrote {len(df)} listings to {csv_path}")

if __name__ == "__main__":
    main()

