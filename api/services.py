import hashlib
import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
 
import numpy as np
import pandas as pd
import requests
from django.conf import settings
from django.core.cache import cache
 
logger = logging.getLogger(__name__)

# Constants
TANK_RANGE_MILES      = 500
MPG                   = 10
ROUTE_PROXIMITY_MILES = 5.0   # stations further than this from the polyline are ignored
GEOCODE_WORKERS       = 6     # parallel Nominatim threads
EARTH_RADIUS_MILES    = 3958.8
NOMINATIM_MIN_INTERVAL = 1.0  # seconds — Nominatim ToS: max 1 req/s
 

# HTTP Session : connection pooling, shared across all threads
_SESSION: Optional[requests.Session] = None
_SESSION_LOCK = threading.Lock()
 
 
def _get_session() -> requests.Session:
    """Return the process-wide requests Session, creating it on first call."""
    global _SESSION
    if _SESSION is None:
        with _SESSION_LOCK:
            if _SESSION is None:
                s = requests.Session()
                adapter = requests.adapters.HTTPAdapter(
                    pool_connections=4,
                    pool_maxsize=10,
                    max_retries=requests.adapters.Retry(
                        total=3,
                        backoff_factor=0.5,
                        status_forcelist=[429, 500, 502, 503, 504],
                    ),
                )
                s.mount("http://", adapter)
                s.mount("https://", adapter)
                s.headers["User-Agent"] = "FuelRouteApp/3.0"
                _SESSION = s
                logger.info("HTTP session created with connection pooling")
    return _SESSION
 
 
# Fuel station data : process-level singleton
_FUEL_DF: Optional[pd.DataFrame] = None
_FUEL_DF_LOCK = threading.Lock()
 
 
def _build_fuel_df() -> pd.DataFrame:
    df = pd.read_csv(settings.FUEL_DATA_PATH)
    df.columns = df.columns.str.strip()
    df = (
        df.sort_values("Retail Price")
          .drop_duplicates(subset=["OPIS Truckstop ID"], keep="first")
          .reset_index(drop=True)
    )
    df["_city_state"] = df["City"].str.strip() + ", " + df["State"].str.strip()
    # Rename spaced columns so itertuples() produces clean attribute names
    df = df.rename(columns={
        "OPIS Truckstop ID": "opis_id",
        "Truckstop Name":    "truckstop_name",
        "Rack ID":           "rack_id",
        "Retail Price":      "retail_price",
    })
    return df
 
 
def load_fuel_stations() -> pd.DataFrame:
    """
    Return the singleton station DataFrame.
    Double-checked locking guarantees thread-safety without lock contention
    after initialisation.
    """
    global _FUEL_DF
    if _FUEL_DF is None:
        with _FUEL_DF_LOCK:
            if _FUEL_DF is None:
                _FUEL_DF = _build_fuel_df()
                logger.info("Fuel station data loaded: %d unique stations", len(_FUEL_DF))
    return _FUEL_DF
 

# Cache key helpers : consistent, collision-free, memcached-safe 
def _geo_key(place: str) -> str:
    safe = place.lower().strip().replace(" ", "_").replace(",", "").replace(".", "")
    return f"geocode:v1:{safe}"
 
 
def _osrm_key(slat: float, slon: float, elat: float, elon: float) -> str:
    # Round inputs to 3 dp (~100m precision) before key generation so that
    # geocodes differing only by floating-point noise map to the same key.
    return f"osrm:v1:{slat:.3f},{slon:.3f},{elat:.3f},{elon:.3f}"
 
 
def _route_key(start: str, end: str) -> str:
    raw = f"{start.lower().strip()}|{end.lower().strip()}"
    return "route:v1:" + hashlib.md5(raw.encode()).hexdigest()
 
 
# Nominatim rate-limit : global token bucket across all threads
_NOMINATIM_LOCK = threading.Lock()
_NOMINATIM_LAST_CALL = 0.0
 
 
def _nominatim_request(place: str) -> list:
    """
    Call Nominatim with a process-wide 1 req/s rate limit enforced by a lock.
    This is safer than per-thread sleep when GEOCODE_WORKERS > 1.
    """
    global _NOMINATIM_LAST_CALL
    with _NOMINATIM_LOCK:
        now     = time.monotonic()
        elapsed = now - _NOMINATIM_LAST_CALL
        if elapsed < NOMINATIM_MIN_INTERVAL:
            time.sleep(NOMINATIM_MIN_INTERVAL - elapsed)
        _NOMINATIM_LAST_CALL = time.monotonic()
 
    resp = _get_session().get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": place + " USA", "format": "json", "limit": 1},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()
 
 
# Geocoding : L1 Django cache → L2 Nominatim
def geocode(place: str) -> tuple[float, float]:
    """
    Geocode a place to (lat, lon), cached for CACHE_TTL_GEOCODE (default 7d).
    Cache hit costs ~0.1ms; miss costs ~500ms network round-trip.
    """
    key = _geo_key(place)
    hit = cache.get(key)
    if hit is not None:
        logger.debug("Geocode cache hit: %s", place)
        return hit
 
    results = _nominatim_request(place)
    if not results:
        raise ValueError(f"Could not geocode '{place}'")
 
    coords = (float(results[0]["lat"]), float(results[0]["lon"]))
    cache.set(key, coords, timeout=getattr(settings, "CACHE_TTL_GEOCODE", 604800))
    logger.debug("Geocoded '%s' → %s", place, coords)
    return coords
 
 
def _geocode_safe(place: str) -> Optional[tuple[float, float]]:
    """Circuit-breaker wrapper: geocoding failure skips the station, not the request."""
    try:
        return geocode(place)
    except Exception as exc:
        logger.warning("Geocode failed for '%s': %s", place, exc)
        return None
 
 
# OSRM routing 
def get_osrm_route(
    start_lat: float, start_lon: float,
    end_lat:   float, end_lon:   float,
) -> tuple[float, float, list]:
    """
    Returns (distance_miles, duration_seconds, [[lon, lat], ...]).
    Result is cached — identical endpoints never hit OSRM twice within TTL.
    """
    key = _osrm_key(start_lat, start_lon, end_lat, end_lon)
    hit = cache.get(key)
    if hit is not None:
        logger.debug("OSRM cache hit")
        return hit
 
    url = (
        f"http://router.project-osrm.org/route/v1/driving/"
        f"{start_lon},{start_lat};{end_lon},{end_lat}"
        f"?overview=full&geometries=geojson&steps=false"
    )
    resp = _get_session().get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()
 
    if data.get("code") != "Ok":
        raise ValueError("OSRM routing failed: " + data.get("message", "unknown"))
 
    route  = data["routes"][0]
    result = (
        route["distance"] / 1609.344,
        route["duration"],
        route["geometry"]["coordinates"],   # [[lon, lat], ...]
    )
    cache.set(key, result, timeout=getattr(settings, "CACHE_TTL_OSRM", 86400))
    return result
 
 
# Vectorised geometry helpers
def _cumulative_distances(lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """
    Fully vectorised cumulative haversine distances along a polyline.
    Returns array of length N where result[i] = miles from point 0 to point i.
    """
    phi1   = np.radians(lats[:-1]);  phi2  = np.radians(lats[1:])
    dphi   = np.radians(lats[1:] - lats[:-1])
    dlam   = np.radians(lons[1:] - lons[:-1])
    a      = np.sin(dphi / 2) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(dlam / 2) ** 2
    segs   = EARTH_RADIUS_MILES * 2 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
    return np.concatenate([[0.0], np.cumsum(segs)])
 
 
def _haversine_to_route(
    slat: float, slon: float,
    route_lats: np.ndarray, route_lons: np.ndarray,
    bbox_margin: float = 0.15,         
) -> tuple[float, int]:
    """
    Minimum distance (miles) from a point to any point on the route polyline,
    and the index of the closest route point.
 
    Two-stage filter:
    1. Bounding-box mask : cheap array comparison, rejects ~95% of points
    2. Vectorised haversine on survivors only
    """
    mask = (
        (np.abs(route_lats - slat) <= bbox_margin) &
        (np.abs(route_lons - slon) <= bbox_margin)
    )
    if not mask.any():
        return float("inf"), -1
 
    sub_lats = route_lats[mask]
    sub_lons = route_lons[mask]
    phi1     = math.radians(slat)
    phi2     = np.radians(sub_lats)
    dphi     = np.radians(sub_lats - slat)
    dlam     = np.radians(sub_lons  - slon)
    a        = np.sin(dphi/2)**2 + math.cos(phi1)*np.cos(phi2)*np.sin(dlam/2)**2
    dists    = EARTH_RADIUS_MILES * 2 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
 
    local_idx  = int(np.argmin(dists))
    global_idx = int(np.where(mask)[0][local_idx])
    return float(dists[local_idx]), global_idx
 
 
# Station enrichment : parallel geocoding + vectorised matching
def enrich_stations_with_coords(
    df: pd.DataFrame,
    route_coords: list,
) -> tuple[pd.DataFrame, float]:
    """
    For each station in df, find its position along the route polyline.
    Returns (stations_on_route sorted by dist_from_start, total_route_miles).
 
    Performance:
    - Cache checked for all cities before any network call
    - Uncached cities geocoded in parallel (up to GEOCODE_WORKERS threads)
    - Global 1 req/s rate limit respected across all threads
    - Proximity matching is fully vectorised
    """
    route_arr  = np.array(route_coords, dtype=np.float64) 
    route_lons = route_arr[:, 0]
    route_lats = route_arr[:, 1]
    cum_dist   = _cumulative_distances(route_lats, route_lons)
    total_miles = float(cum_dist[-1])
 
    # Batch cache lookup before spawning any threads
    unique_places = df["_city_state"].unique().tolist()
    place_coords: dict[str, Optional[tuple[float, float]]] = {}
    uncached: list[str] = []
 
    for p in unique_places:
        hit = cache.get(_geo_key(p))
        if hit is not None:
            place_coords[p] = hit
        else:
            uncached.append(p)
 
    logger.debug(
        "Station geocoding: %d cached, %d to fetch", len(place_coords), len(uncached)
    )
 
    # Parallel geocoding for cache misses 
    if uncached:
        with ThreadPoolExecutor(max_workers=GEOCODE_WORKERS) as pool:
            future_to_place = {pool.submit(_geocode_safe, p): p for p in uncached}
            for fut in as_completed(future_to_place):
                place = future_to_place[fut]
                place_coords[place] = fut.result()   # None on failure 
 
    # Vectorised proximity matching --
    records = []
    for row in df.itertuples(index=False):
        coords = place_coords.get(row._asdict().get("_city_state"))
        if coords is None:
            continue
        slat, slon = coords
 
        min_dist, route_idx = _haversine_to_route(slat, slon, route_lats, route_lons)
        if min_dist > ROUTE_PROXIMITY_MILES:
            continue
 
        records.append({
            "id":              row.opis_id,
            "name":            row.truckstop_name,
            "city":            row.City,
            "state":           row.State,
            "address":         row.Address,
            "price":           float(row.retail_price),
            "lat":             slat,
            "lon":             slon,
            "dist_from_start": float(cum_dist[route_idx]),
            "dist_from_route": round(min_dist, 3),
        })
 
    result = pd.DataFrame(records)
    if not result.empty:
        result = result.sort_values("dist_from_start").reset_index(drop=True)
    return result, total_miles
 

# Optimal fuel selection algorithm
def pick_fuel_stops(stations_df: pd.DataFrame, total_miles: float) -> list[dict]:
    """
    Provably optimal fuel selection: 'gas station look-ahead' algorithm.
 
    Two rules govern every decision at each station:
 
      Rule A — We are the cheapest station within a full tank's reach:
               Fill completely. The price only gets worse from here.
 
      Rule B — A cheaper station is reachable with current fuel:
               Buy only enough to reach the nearest cheaper station.
               Avoid paying the current (higher) price for excess gallons.
 
      Destination rule — No cheaper stations ahead within range:
               Buy just enough to reach the destination (not a full tank).
               Overfilling at any expensive station wastes money.

 
    Time complexity: O(n²) where n = on-route stations.
    """
    if stations_df.empty:
        return []
 
    stations = stations_df.to_dict("records")   # list for O(1) index access
    stops    = []
    fuel     = float(TANK_RANGE_MILES)          # start with a full tank
    pos      = 0.0
    i        = 0
 
    while pos < total_miles and i < len(stations):
        curr = stations[i]
 
        # Cannot reach this station and handles data gaps gracefully
        if curr["dist_from_start"] > pos + fuel + 1e-6:
            i += 1
            continue
 
        # Drive to this station
        driven = curr["dist_from_start"] - pos
        fuel  -= driven
        pos    = curr["dist_from_start"]
 
        # Already have enough fuel to reach the destination — no stop needed
        if pos + fuel >= total_miles:
            i += 1
            continue
 
        # Identify cheaper stations within a full tank from here
        horizon = pos + TANK_RANGE_MILES
        ahead   = [s for s in stations[i + 1:] if s["dist_from_start"] <= horizon]
        cheaper = [s for s in ahead if s["price"] < curr["price"]]
 
        if cheaper:
            # Rule B: buy only enough to reach the nearest cheaper station
            nearest_cheaper = min(cheaper, key=lambda s: s["dist_from_start"])
            needed  = nearest_cheaper["dist_from_start"] - pos
            gallons = max(0.0, (needed - fuel) / MPG)
        else:
            # Rule A / Destination rule: fill as much as needed
            # Cap at what's needed to reach destination (never overfill)
            miles_needed = min(total_miles - pos, TANK_RANGE_MILES)
            gallons      = max(0.0, (miles_needed - fuel) / MPG)
 
        if gallons > 0.001:
            cost = round(gallons * curr["price"], 2)
            stops.append({
                **curr,
                "gallons_purchased":     round(gallons, 2),
                "cost_at_stop":          cost,
                "fuel_on_arrival_miles": round(fuel, 1),
            })
            fuel += gallons * MPG
 
        i += 1
 
    return stops
 
 
# Public API entrypoint
def plan_route(start: str, end: str) -> dict:
    """
    The full pipeline.
 
    Cache hierarchy:
      L1 — full result (1h TTL): cache hit returns in < 1ms
      L2 — OSRM route (1d TTL): skips the one external routing call
      L3 — geocodes (7d TTL): skips Nominatim calls for known cities
      L4 — recompute: all layers cold, ~2-5s depending on network
 
    The returned dict is a COPY of the cached value and is never mutated in place
    to avoid poisoning the cache with state changes (e.g. _cached flag).
    """
    result_key    = _route_key(start, end)
    cached_result = cache.get(result_key)
    if cached_result is not None:
        logger.debug("plan_route full-result cache hit: %s → %s", start, end)
        # Return a shallow copy
        return {**cached_result, "_cached": True}
 
    # Geocode start + end (individually cached) 
    start_lat, start_lon = geocode(start)
    end_lat,   end_lon   = geocode(end)
 
    # Single OSRM call (cached)
    total_miles, duration_sec, route_coords = get_osrm_route(
        start_lat, start_lon, end_lat, end_lon
    )
 
    # Station data (process singleton) --
    fuel_df = load_fuel_stations()
 
    # Match stations to route --
    stations_on_route, _ = enrich_stations_with_coords(fuel_df, route_coords)
 
    # Optimal fuel stop selection --
    stops      = pick_fuel_stops(stations_on_route, total_miles)
    total_cost = round(sum(s["cost_at_stop"] for s in stops), 2)
 
    hours   = int(duration_sec // 3600)
    minutes = int((duration_sec % 3600) // 60)
 
    result = {
        "_cached": False,
        "origin":      {"name": start, "lat": start_lat, "lon": start_lon},
        "destination": {"name": end,   "lat": end_lat,   "lon": end_lon},
        "route_summary": {
            "total_miles":          round(total_miles, 1),
            "estimated_drive_time": f"{hours}h {minutes}m",
            "vehicle_mpg":          MPG,
            "tank_range_miles":     TANK_RANGE_MILES,
            "total_gallons_needed": round(total_miles / MPG, 2),
            "total_fuel_cost_usd":  total_cost,
            "fuel_stops_count":     len(stops),
        },
        "fuel_stops": [
            {
                "stop_number":        i + 1,
                "name":               s["name"],
                "address":            s["address"],
                "city":               s["city"],
                "state":              s["state"],
                "lat":                round(s["lat"], 6),
                "lon":                round(s["lon"], 6),
                "price_per_gallon":   round(s["price"], 3),
                "gallons_purchased":  s["gallons_purchased"],
                "cost_at_stop":       s["cost_at_stop"],
                "miles_from_start":   round(s["dist_from_start"], 1),
                "fuel_on_arrival_miles": s.get("fuel_on_arrival_miles", 0),
            }
            for i, s in enumerate(stops)
        ],
        "route_geometry": {
            "type":        "LineString",
            # Downsample only at serialisation
            "coordinates": route_coords[::10],
        },
    }
 
    cache.set(result_key, result, timeout=getattr(settings, "CACHE_TTL_ROUTE", 3600))
    return result
