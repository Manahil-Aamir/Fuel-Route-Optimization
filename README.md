# Fuel Route Optimizer — Django REST API

A Django REST API that finds the **cheapest fuel stops** along a US road trip route, given a 500-mile tank range and 10 MPG vehicle.

---

## Architecture

```
POST /api/route/  { "start": "...", "end": "..." }
       │
       ├─ 1. Geocode start + end (Nominatim, free, no API key)
       ├─ 2. Get driving route (OSRM, free, no API key) ← single API call
       ├─ 3. Load fuel_prices_with_coords.csv (in-memory, pandas)
       ├─ 4. Match stations to route (haversine distance, local computation)
       ├─ 5. Greedy cheapest-stop algorithm (local)
       └─ 6. Return JSON: stops, geometry, total cost
```

**External API calls: 3 total** (2 geocodes + 1 OSRM route)  
Station coordinates are pre-baked into the CSV — no per-request geocoding of fuel stations.

---

## Setup

### Requirements

- Python 3.10+
- Django 6.x
- Django REST Framework
- pandas
- numpy
- requests

### Install

```bash
git clone <repo>
cd fuel_route

pip install django djangorestframework pandas numpy requests
```

### One-time data preparation (required before first run)

The fuel station CSV must have lat/lon coordinates baked in before the server starts. Run this script once from the project root:

```bash
python prebake_coords.py
```

This reads `fuel_prices.csv`, geocodes every unique city/state via Nominatim (respecting the 1 req/s rate limit), and writes `fuel_prices_with_coords.csv` with `lat` and `lon` columns added. Progress is saved to `geocode_cache.txt` so it can be safely interrupted and resumed.

> **How long does it take?** About 25 minutes for ~1,400 unique cities. You only ever run this once.

### Configure settings

In `settings.py`, point to the new CSV:

```python
FUEL_DATA_PATH = BASE_DIR / "fuel_prices_with_coords.csv"
```

### Run the server

```bash
python manage.py check
python manage.py runserver
```

---

## API Usage

### `GET /api/route/`

Returns usage instructions.

### `POST /api/route/`

**Request body:**

```json
{
  "start": "Chicago, IL",
  "end": "Los Angeles, CA"
}
```

**Response:**

```json
{
  "origin": {
    "name": "Chicago, IL",
    "lat": 41.85,
    "lon": -87.65
  },
  "destination": {
    "name": "Los Angeles, CA",
    "lat": 34.05,
    "lon": -118.24
  },
  "route_summary": {
    "total_miles": 2015.3,
    "estimated_drive_time": "30h 15m",
    "vehicle_mpg": 10,
    "tank_range_miles": 500,
    "total_gallons_needed": 201.53,
    "total_fuel_cost_usd": 612.45,
    "fuel_stops_count": 4
  },
  "fuel_stops": [
    {
      "stop_number": 1,
      "name": "FLYING J #1023",
      "address": "I-40, EXIT 335 & SR-83",
      "city": "Elk City",
      "state": "OK",
      "lat": 35.41,
      "lon": -99.43,
      "price_per_gallon": 2.969,
      "gallons_purchased": 32.5,
      "cost_at_stop": 96.49,
      "miles_from_start": 401.2
    }
  ],
  "route_geometry": {
    "type": "LineString",
    "coordinates": [[-87.65, 41.85], "..."]
  }
}
```

The `route_geometry` field is a GeoJSON LineString — paste it into [geojson.io](https://geojson.io) to visualize the route instantly.

---

## Optimization Algorithm

The fuel stop selector uses a **greedy look-ahead** approach with two rules:

- **Rule A** — Current station is the cheapest within a full tank's reach: fill completely.
- **Rule B** — A cheaper station is reachable with current fuel: buy only enough to reach it.
- **Destination rule** — No cheaper stations ahead: buy just enough to reach the destination, no more.

This runs in O(n²) where n = stations on route, which is fast in practice (typically < 50 stations per route).

### Station matching

Stations are matched to the route using coordinates already stored in the CSV:

1. Load `lat`/`lon` directly from `fuel_prices_with_coords.csv` (no geocoding at runtime)
2. Find the nearest point on the OSRM polyline using vectorised haversine distance
3. Keep only stations within **5 miles** of the route

This is why running `prebake_coords.py` first is required — without pre-baked coordinates, station matching cannot work and all routes return $0 cost.

---

## Cache Hierarchy

Responses are fast after the first request thanks to a four-level cache:

| Level | What's cached        | TTL      |
| ----- | -------------------- | -------- |
| L1    | Full route result    | 1 hour   |
| L2    | OSRM route polyline  | 24 hours |
| L3    | Start/end geocodes   | 7 days   |
| L4    | Nothing — recomputes | —        |

First request for a new route takes ~2–3 seconds (2 Nominatim calls + 1 OSRM call). Every repeat request returns in under 1ms.

---

## Running Tests

```bash
python manage.py test api --verbosity=2
```

12 tests covering haversine distance, CSV loading and deduplication, fuel stop selection edge cases, and full integration with mocked external APIs.

---

## Free APIs Used

| API                                               | Purpose                    | Key required? | Calls per route |
| ------------------------------------------------- | -------------------------- | ------------- | --------------- |
| [Nominatim](https://nominatim.openstreetmap.org/) | Geocode start + end cities | No            | 2               |
| [OSRM Demo](http://router.project-osrm.org/)      | Driving route + polyline   | No            | 1               |

> **Note:** Nominatim's ToS requires max 1 req/sec and a valid `User-Agent`. Both are enforced by a process-wide rate-limit lock. For production, self-host Nominatim or switch to a commercial geocoder.

---

## Design Decisions

- **Pre-baked coordinates** — fuel station lat/lon is stored in the CSV rather than geocoded at runtime, eliminating thousands of Nominatim calls per cold start and making every route request fast
- **One OSRM call** returns full geometry + distance + duration — no multiple waypoint calls needed
- **Process-level singletons** — the CSV DataFrame and HTTP session are initialised once and reused across all requests
- **Vectorised matching** — station-to-route proximity uses numpy broadcasting with a bounding-box pre-filter, rejecting ~95% of stations before the haversine calculation
- **Geometry downsampled** (every 10th point) in the response to keep payload small while preserving route shape
- **Deduplication** of truckstop IDs keeps the cheapest price when a station appears multiple times in the CSV

---

## Project Structure

```
fuel_route/
├── api/                        # Django app
│   ├── services.py             # All business logic (geocoding, routing, algorithm)
│   ├── views.py                # DRF views
│   ├── urls.py
│   └── tests.py
├── config/                     # Django project settings
│   ├── settings.py
│   └── urls.py
├── fuel_prices.csv             # Original OPIS data (source)
├── fuel_prices_with_coords.csv # Generated by prebake_coords.py (required to run)
├── prebake_coords.py           # One-time coord baking script
├── geocode_cache.txt           # Auto-generated resume file for prebake script
└── manage.py
```
