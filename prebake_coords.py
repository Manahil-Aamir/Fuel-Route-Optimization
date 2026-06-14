import time
import csv
import requests
from pathlib import Path
 
INPUT_CSV  = "fuel_prices.csv"
OUTPUT_CSV = "fuel_prices_with_coords.csv"
CACHE_FILE = "geocode_cache.txt"   # resume support — already-done cities saved here
DELAY      = 1.1                   # seconds between Nominatim calls (ToS: max 1/s)
 
session = requests.Session()
session.headers["User-Agent"] = "FuelRouteApp/3.0 prebake-script"
 
 
def nominatim_geocode(city: str, state: str) -> tuple[float, float] | None:
    place = f"{city.strip()}, {state.strip()} USA"
    try:
        r = session.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": place, "format": "json", "limit": 1},
            timeout=10,
        )
        r.raise_for_status()
        results = r.json()
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception as e:
        print(f"  FAILED {place}: {e}")
    return None
 
 
def load_cache(cache_file: str) -> dict[str, tuple[float, float]]:
    cache = {}
    path = Path(cache_file)
    if path.exists():
        with open(path) as f:
            for line in f:
                parts = line.strip().split("\t")
                if len(parts) == 3:
                    cache[parts[0]] = (float(parts[1]), float(parts[2]))
    print(f"Loaded {len(cache)} cached geocodes from {cache_file}")
    return cache
 
 
def save_to_cache(cache_file: str, key: str, lat: float, lon: float):
    with open(cache_file, "a") as f:
        f.write(f"{key}\t{lat}\t{lon}\n")
 
 
def main():
    # Load existing geocode cache (so you can resume if interrupted)
    geocode_cache = load_cache(CACHE_FILE)
 
    # Read input CSV
    with open(INPUT_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames
 
    print(f"Loaded {len(rows)} rows from {INPUT_CSV}")
 
    # Find unique city+state combos
    unique_places = {}
    for row in rows:
        key = f"{row['City'].strip()}, {row['State'].strip()}"
        unique_places[key] = (row["City"].strip(), row["State"].strip())
 
    print(f"Found {len(unique_places)} unique city/state combos")
 
    # Geocode only the ones not already cached
    to_fetch = [k for k in unique_places if k not in geocode_cache]
    print(f"Need to geocode {len(to_fetch)} new cities (will take ~{len(to_fetch)//60} mins)")
 
    for i, key in enumerate(to_fetch):
        city, state = unique_places[key]
        print(f"  [{i+1}/{len(to_fetch)}] {key} ...", end=" ", flush=True)
        coords = nominatim_geocode(city, state)
        if coords:
            geocode_cache[key] = coords
            save_to_cache(CACHE_FILE, key, coords[0], coords[1])
            print(f"→ {coords[0]:.4f}, {coords[1]:.4f}")
        else:
            print("→ NOT FOUND (will be skipped)")
        time.sleep(DELAY)
 
    # Write output CSV with lat/lon columns added
    new_fieldnames = list(fieldnames) + ["lat", "lon"]
    found = 0
    missing = 0
 
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=new_fieldnames)
        writer.writeheader()
        for row in rows:
            key = f"{row['City'].strip()}, {row['State'].strip()}"
            coords = geocode_cache.get(key)
            if coords:
                row["lat"] = round(coords[0], 6)
                row["lon"] = round(coords[1], 6)
                found += 1
            else:
                row["lat"] = ""
                row["lon"] = ""
                missing += 1
            writer.writerow(row)
 
    print(f"\nDone! Written to {OUTPUT_CSV}")
    print(f"  Stations with coords: {found}")
    print(f"  Stations without coords (will be skipped): {missing}")
 
 
if __name__ == "__main__":
    main()