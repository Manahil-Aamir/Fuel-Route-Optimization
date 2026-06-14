# Fuel Route Optimizer — Django REST API

A Django REST API that finds the **cheapest fuel stops** along a US road trip route, given a 500-mile tank range and 10 MPG vehicle.

---

## Architecture

```
POST /api/route/  { "start": "...", "end": "..." }
       │
       ├─ 1. Geocode start + end (Nominatim, free, no API key)
       ├─ 2. Get driving route (OSRM, free, no API key) ← single API call
       ├─ 3. Load OPIS fuel price CSV (in-memory, pandas)
       ├─ 4. Match stations to route (haversine distance, local computation)
       ├─ 5. Greedy cheapest-stop algorithm (local)
       └─ 6. Return JSON: stops, geometry, total cost
```

**External API calls: 3 total** (2 geocodes + 1 OSRM route)  
Nominatim city-level geocoding is cached within a request to avoid repeat calls.

---

## Setup

### Requirements

- Python 3.10+
- Django 6.x
- Django REST Framework
- pandas

### Install

```bash
git clone <repo>
cd fuel_route

pip install django djangorestframework pandas

python manage.py check
python manage.py runserver
```

The `fuel_prices.csv` file must be in the project root (next to `manage.py`).

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
    // ...more stops
  ],
  "route_geometry": {
    "type": "LineString",
    "coordinates": [[-87.65, 41.85], ...]
  }
}
```

The `route_geometry` field is a GeoJSON LineString — paste it into [geojson.io](https://geojson.io) to visualize the route instantly.

---

## Optimization Algorithm

The fuel stop selector uses a **greedy look-ahead** approach:

1. Start with a full tank (500 miles of range)
2. At each position, find all stations reachable with current fuel
3. If the destination is reachable → done (no stop needed)
4. Otherwise, pick the **cheapest** reachable station and fill up completely
5. Repeat until destination is reachable

This runs in O(n²) worst case where n = stations on route, which is fast in practice (typically < 50 stations per route).

### Station matching

Stations from `fuel_prices.csv` are matched to the route by:
1. Geocoding each city/state (Nominatim, with in-request caching)
2. Finding the nearest point on the OSRM polyline using haversine distance
3. Keeping only stations within **5 miles** of the route

---

## Running Tests

```bash
python manage.py test api --verbosity=2
```

12 tests covering:
- Haversine distance formula
- CSV loading + deduplication
- Fuel stop selection logic (edge cases: empty, short trips, cheapest selection)
- Full integration (mocked external APIs)

---

## Free APIs Used

| API | Purpose | Key required? | Calls per route |
|-----|---------|---------------|-----------------|
| [Nominatim](https://nominatim.openstreetmap.org/) | Geocoding | No | 2 (start + end) + city lookups |
| [OSRM Demo](http://router.project-osrm.org/) | Driving route + polyline | No | **1** |

> **Note:** Nominatim's ToS requires max 1 req/sec and a valid `User-Agent`. Both are respected in this implementation. For production, self-host Nominatim or use a commercial geocoder.

---

## Design Decisions

- **One OSRM call** returns full geometry + distance + duration — no need for multiple waypoint calls
- **CSV loaded once per request** via pandas; for production, load at startup or cache with Django's `cache` framework
- **Geometry downsampled** (every 10th point) in the response to keep payload small while preserving shape
- **Deduplication** of truckstop IDs keeps cheapest price when a station appears multiple times in the CSV
