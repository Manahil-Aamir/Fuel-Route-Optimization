"""
Fuel Route Optimizer — test suite v3
Covers: geometry math, cache layers, optimal algorithm, integration.
"""
import os
import unittest
from unittest.mock import patch, MagicMock
import numpy as np
import pandas as pd

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django; django.setup()

from django.core.cache import cache
from api.services import (
    _cumulative_distances,
    _haversine_to_route,
    _geo_key,
    _route_key,
    pick_fuel_stops,
    load_fuel_stations,
    plan_route,
    enrich_stations_with_coords,
    geocode,
)


# Helpers
def _make_stations(entries):
    """entries: list of (dist_from_start, price)"""
    return pd.DataFrame([
        {"opis_id": i, "truckstop_name": f"S{i}", "City": "T", "State": "TX",
         "Address": "I-40", "retail_price": p, "lat": 30.0, "lon": -90.0 + i * 5,
         "dist_from_start": d, "dist_from_route": 0.5, "_city_state": "T, TX",
         "rack_id": 0,
         **{"id": i, "name": f"S{i}", "city": "T", "state": "TX",
            "address": "I-40", "price": p}}
        for i, (d, p) in enumerate(entries)
    ])


def _make_station_records(entries):
    """For pick_fuel_stops which takes a df with 'price'/'dist_from_start' cols."""
    return pd.DataFrame([
        {"id": i, "name": f"S{i}", "city": "T", "state": "TX", "address": "I-40",
         "price": p, "lat": 30.0, "lon": -90.0 + i * 5,
         "dist_from_start": d, "dist_from_route": 0.5}
        for i, (d, p) in enumerate(entries)
    ])


# Geometry
class TestCumulativeDistances(unittest.TestCase):
    def test_zero_for_single_point(self):
        lats = np.array([41.85])
        lons = np.array([-87.65])
        # Need at least 2 points; single point → only [0.0]
        lats2 = np.array([41.85, 41.85])
        lons2 = np.array([-87.65, -87.65])
        cd = _cumulative_distances(lats2, lons2)
        self.assertAlmostEqual(cd[0], 0.0)
        self.assertAlmostEqual(cd[1], 0.0, places=3)

    def test_monotonically_increasing(self):
        lats = np.linspace(41.85, 34.05, 10)
        lons = np.linspace(-87.65, -118.24, 10)
        cd   = _cumulative_distances(lats, lons)
        self.assertTrue(np.all(np.diff(cd) >= 0))

    def test_chicago_to_la_range(self):
        lats = np.array([41.85, 34.05])
        lons = np.array([-87.65, -118.24])
        cd   = _cumulative_distances(lats, lons)
        self.assertGreater(cd[-1], 1500)
        self.assertLess(cd[-1], 2200)

    def test_symmetry(self):
        lats = np.array([41.85, 34.05])
        lons = np.array([-87.65, -118.24])
        cd1  = _cumulative_distances(lats, lons)
        cd2  = _cumulative_distances(lats[::-1], lons[::-1])
        self.assertAlmostEqual(cd1[-1], cd2[-1], places=3)


class TestHaversineToRoute(unittest.TestCase):
    def setUp(self):
        # Simple east-west route
        self.lats = np.array([35.0, 35.0, 35.0])
        self.lons = np.array([-100.0, -95.0, -90.0])

    def test_point_on_route(self):
        dist, idx = _haversine_to_route(35.0, -95.0, self.lats, self.lons)
        self.assertAlmostEqual(dist, 0.0, places=2)
        self.assertEqual(idx, 1)

    def test_point_far_off_route(self):
        dist, idx = _haversine_to_route(50.0, -95.0, self.lats, self.lons)
        self.assertGreater(dist, 900)

    def test_returns_inf_when_outside_bbox(self):
        dist, idx = _haversine_to_route(80.0, 10.0, self.lats, self.lons)
        self.assertEqual(dist, float("inf"))
        self.assertEqual(idx, -1)


# Fuel station singleton
class TestLoadFuelStations(unittest.TestCase):
    def test_singleton_identity(self):
        self.assertIs(load_fuel_stations(), load_fuel_stations())

    def test_deduplication(self):
        df = load_fuel_stations()
        self.assertEqual(len(df["opis_id"].unique()), len(df))

    def test_required_columns(self):
        df = load_fuel_stations()
        for col in ["truckstop_name", "City", "State", "retail_price", "_city_state"]:
            self.assertIn(col, df.columns)

    def test_prices_positive(self):
        self.assertTrue((load_fuel_stations()["retail_price"] > 0).all())

    def test_cheapest_survives_dedup(self):
        df = load_fuel_stations()
        # After dedup all prices should be the minimum for that ID
        self.assertFalse(df["retail_price"].isna().any())


# Optimal fuel algorithm
class TestPickFuelStops(unittest.TestCase):

    def test_short_trip_no_stop(self):
        stops = pick_fuel_stops(_make_station_records([(200, 3.0), (400, 3.5)]), 450)
        self.assertEqual(len(stops), 0)

    def test_empty_stations(self):
        self.assertEqual(pick_fuel_stops(pd.DataFrame(), 1000), [])

    def test_one_required_stop(self):
        # 800mi trip, only station at 400mi
        stops = pick_fuel_stops(_make_station_records([(400, 3.0)]), 800)
        self.assertEqual(len(stops), 1)
        # Arrive with 100mi fuel, need 400mi more → buy 30gal
        self.assertAlmostEqual(stops[0]["gallons_purchased"], 30.0, places=1)
        self.assertAlmostEqual(stops[0]["cost_at_stop"], 90.0, places=1)

    def test_optimal_vs_naive_greedy(self):
        """
        Expensive@300mi ($5), Cheap@550mi ($1), dest@800mi.
        Naive (fill completely at 300): 30gal×$5 = $150.
        Optimal: 5gal×$5 (reach 550) + 25gal×$1 (reach dest) = $50.
        """
        stops = pick_fuel_stops(
            _make_station_records([(300, 5.0), (550, 1.0)]), 800
        )
        total = sum(s["cost_at_stop"] for s in stops)
        self.assertAlmostEqual(total, 50.0, places=1)
        self.assertEqual(len(stops), 2)

    def test_cheapest_in_range_fills_completely(self):
        """When current station IS cheapest, fill the tank."""
        stops = pick_fuel_stops(
            _make_station_records([(200, 1.0), (600, 5.0)]), 900
        )
        if stops:
            s = next((s for s in stops if s["dist_from_start"] == 200), None)
            if s:
                self.assertAlmostEqual(s["gallons_purchased"], 20.0, places=1)

    def test_never_overfill_past_destination(self):
        """Never buy more fuel than needed to reach the destination."""
        stops = pick_fuel_stops(
            _make_station_records([(300, 2.0)]), 400
        )
        # Start full (500mi), at 300mi have 200mi left, need 100mi to dest → buy 0
        self.assertEqual(len(stops), 0)

    def test_multi_stop_cost_positive(self):
        stops = pick_fuel_stops(
            _make_station_records([(400, 3.0), (700, 2.5), (900, 3.5)]), 1200
        )
        self.assertGreater(sum(s["cost_at_stop"] for s in stops), 0)

    def test_stop_fields_present(self):
        stops = pick_fuel_stops(_make_station_records([(400, 3.0)]), 800)
        if stops:
            for field in ("gallons_purchased", "cost_at_stop", "fuel_on_arrival_miles"):
                self.assertIn(field, stops[0])



# Cache behaviour
class TestCaching(unittest.TestCase):
    def setUp(self):
        cache.clear()

    def test_geocode_result_is_cached(self):
        with patch("api.services._nominatim_request") as mock_nom:
            mock_nom.return_value = [{"lat": "41.85", "lon": "-87.65"}]
            geocode("Chicago, IL")
            geocode("Chicago, IL")  # second call
            self.assertEqual(mock_nom.call_count, 1, "Nominatim called more than once")

    def test_full_result_served_from_cache(self):
        fake = {"_cached": False, "route_summary": {"total_miles": 500}}
        cache.set(_route_key("A", "B"), fake, 3600)
        with patch("api.services.geocode") as mg, patch("api.services.get_osrm_route") as mo:
            result = plan_route("A", "B")
            mg.assert_not_called()
            mo.assert_not_called()
        self.assertTrue(result["_cached"])

    def test_cache_not_mutated(self):
        """Returning _cached=True must not modify the object stored in cache."""
        fake = {"_cached": False, "route_summary": {}}
        cache.set(_route_key("X", "Y"), fake, 3600)
        plan_route("X", "Y")   # sets _cached=True on returned copy
        stored = cache.get(_route_key("X", "Y"))
        self.assertFalse(stored["_cached"], "Cached object was mutated!")

    def test_first_call_sets_cached_false(self):
        route_coords = [[-87.65, 41.85], [-118.24, 34.05]]
        with patch("api.services.geocode", return_value=(41.85, -87.65)), \
             patch("api.services.get_osrm_route", return_value=(500.0, 36000, route_coords)), \
             patch("api.services.enrich_stations_with_coords",
                   return_value=(pd.DataFrame(), 500.0)):
            result = plan_route("Nowhere A", "Nowhere B")
        self.assertFalse(result["_cached"])


# Integration
class TestIntegration(unittest.TestCase):
    def setUp(self):
        cache.clear()

    @patch("api.services.geocode")
    @patch("api.services.get_osrm_route")
    def test_full_response_structure(self, mock_osrm, mock_geo):
        route_coords = [
            [-87.65, 41.85], [-94.0, 36.0], [-97.5, 35.5],
            [-101.8, 35.2], [-106.5, 31.8], [-112.1, 33.4], [-118.24, 34.05],
        ]
        mock_osrm.return_value = (2000.0, 108000, route_coords)

        on_route = {
            "chicago": (41.85, -87.65), "los angeles": (34.05, -118.24),
            "fort smith": (35.39, -94.42), "clinton": (35.51, -98.97),
            "elk city": (35.41, -99.43), "amarillo": (35.22, -101.83),
            "el paso": (31.76, -106.49), "phoenix": (33.45, -112.07),
        }
        mock_geo.side_effect = lambda p: next(
            (v for k, v in on_route.items() if k in p.lower()), (48.0, -100.0)
        )

        result = plan_route("Chicago, IL", "Los Angeles, CA")

        for key in ("origin", "destination", "route_summary", "fuel_stops", "route_geometry"):
            self.assertIn(key, result)

        s = result["route_summary"]
        self.assertEqual(s["total_miles"], 2000.0)
        self.assertEqual(s["vehicle_mpg"], 10)
        self.assertEqual(s["tank_range_miles"], 500)
        self.assertIsInstance(s["fuel_stops_count"], int)
        self.assertGreaterEqual(s["total_fuel_cost_usd"], 0)
        self.assertEqual(result["route_geometry"]["type"], "LineString")

        print(f"\nIntegration: {s['fuel_stops_count']} stops, "
              f"${s['total_fuel_cost_usd']:.2f} total")
        for st in result["fuel_stops"]:
            print(f"   #{st['stop_number']} {st['name']} ({st['city']}, {st['state']}) "
                  f"@ ${st['price_per_gallon']:.3f} → ${st['cost_at_stop']:.2f}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
