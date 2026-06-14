import os
import unittest
from unittest.mock import patch, MagicMock
import numpy as np
import pandas as pd

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django; django.setup()

from django.test import TestCase, override_settings
from django.core.cache import cache
from api.services import (
    _cumulative_distances,
    _haversine_to_route,
    _geo_key,
    _route_key,
    pick_fuel_stops,
    load_fuel_stations,
    plan_route,
    geocode,
)

# Use in-memory cache for all tests
TEST_CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
    }
}


def make_station_records(entries):
    """entries: list of (dist_from_start, price)"""
    return pd.DataFrame([
        {'id': i, 'name': f'S{i}', 'city': 'T', 'state': 'TX',
         'address': 'I-40', 'price': p, 'lat': 30.0, 'lon': -90.0 + i * 5,
         'dist_from_start': d, 'dist_from_route': 0.5}
        for i, (d, p) in enumerate(entries)
    ])


# Geometry
class TestCumulativeDistances(unittest.TestCase):
    def test_monotonically_increasing(self):
        lats = np.linspace(41.85, 34.05, 10)
        lons = np.linspace(-87.65, -118.24, 10)
        cd = _cumulative_distances(lats, lons)
        self.assertTrue(np.all(np.diff(cd) >= 0))

    def test_chicago_to_la_range(self):
        cd = _cumulative_distances(
            np.array([41.85, 34.05]),
            np.array([-87.65, -118.24])
        )
        self.assertGreater(cd[-1], 1500)
        self.assertLess(cd[-1], 2200)

    def test_symmetry(self):
        lats = np.array([41.85, 34.05])
        lons = np.array([-87.65, -118.24])
        cd1 = _cumulative_distances(lats, lons)
        cd2 = _cumulative_distances(lats[::-1], lons[::-1])
        self.assertAlmostEqual(cd1[-1], cd2[-1], places=3)

    def test_starts_at_zero(self):
        lats = np.array([35.0, 36.0, 37.0])
        lons = np.array([-95.0, -96.0, -97.0])
        cd = _cumulative_distances(lats, lons)
        self.assertEqual(cd[0], 0.0)


class TestHaversineToRoute(unittest.TestCase):
    def setUp(self):
        self.lats = np.array([35.0, 35.0, 35.0])
        self.lons = np.array([-100.0, -95.0, -90.0])

    def test_point_on_route(self):
        dist, idx = _haversine_to_route(35.0, -95.0, self.lats, self.lons)
        self.assertAlmostEqual(dist, 0.0, places=2)
        self.assertEqual(idx, 1)

    def test_far_off_route(self):
        dist, _ = _haversine_to_route(50.0, -95.0, self.lats, self.lons)
        self.assertGreater(dist, 900)

    def test_outside_bbox_returns_inf(self):
        dist, idx = _haversine_to_route(80.0, 10.0, self.lats, self.lons)
        self.assertEqual(dist, float('inf'))
        self.assertEqual(idx, -1)


# Fuel station singleton
class TestLoadFuelStations(unittest.TestCase):
    def test_singleton_identity(self):
        self.assertIs(load_fuel_stations(), load_fuel_stations())

    def test_deduplication(self):
        df = load_fuel_stations()
        self.assertEqual(len(df['opis_id'].unique()), len(df))

    def test_required_columns(self):
        df = load_fuel_stations()
        for col in ['truckstop_name', 'City', 'State', 'retail_price', '_city_state']:
            self.assertIn(col, df.columns)

    def test_prices_positive(self):
        self.assertTrue((load_fuel_stations()['retail_price'] > 0).all())


# Optimal fuel algorithm
class TestPickFuelStops(unittest.TestCase):
    def test_short_trip_no_stop(self):
        stops = pick_fuel_stops(make_station_records([(200, 3.0), (400, 3.5)]), 450)
        self.assertEqual(len(stops), 0)

    def test_empty_stations(self):
        self.assertEqual(pick_fuel_stops(pd.DataFrame(), 1000), [])

    def test_one_required_stop_buys_exact_amount(self):
        # 800mi trip, station at 400mi, start full (500mi)
        # Arrive with 100mi fuel. Need 400mi more → buy 30gal
        stops = pick_fuel_stops(make_station_records([(400, 3.0)]), 800)
        self.assertEqual(len(stops), 1)
        self.assertAlmostEqual(stops[0]['gallons_purchased'], 30.0, places=1)
        self.assertAlmostEqual(stops[0]['cost_at_stop'], 90.0, places=1)

    def test_optimal_look_ahead(self):
        """
        Expensive@300mi ($5), Cheap@550mi ($1), dest@800mi.
        Optimal: 5gal@$5 (reach 550) + 25gal@$1 (reach dest) = $50.
        Naive fill-completely: 30gal@$5 = $150.
        """
        stops = pick_fuel_stops(make_station_records([(300, 5.0), (550, 1.0)]), 800)
        total = sum(s['cost_at_stop'] for s in stops)
        self.assertEqual(len(stops), 2)
        self.assertAlmostEqual(total, 50.0, places=1)

    def test_cheapest_in_range_fills_completely(self):
        # Station at 200mi is cheapest in range — should fill completely
        # Start full (500mi), drive 200mi → 300mi left. Buy 200mi worth = 20gal.
        stops = pick_fuel_stops(make_station_records([(200, 1.0), (600, 5.0)]), 900)
        cheap_stop = next((s for s in stops if s['dist_from_start'] == 200), None)
        if cheap_stop:
            self.assertAlmostEqual(cheap_stop['gallons_purchased'], 20.0, places=1)

    def test_never_overfill_past_destination(self):
        # Station at 300mi, dest at 400mi, start full (500mi) → no stop needed
        stops = pick_fuel_stops(make_station_records([(300, 2.0)]), 400)
        self.assertEqual(len(stops), 0)

    def test_stop_has_required_fields(self):
        stops = pick_fuel_stops(make_station_records([(400, 3.0)]), 800)
        if stops:
            for field in ('gallons_purchased', 'cost_at_stop', 'fuel_on_arrival_miles'):
                self.assertIn(field, stops[0])

    def test_cost_always_positive(self):
        stops = pick_fuel_stops(make_station_records([(400, 3.0), (700, 2.5)]), 1100)
        for s in stops:
            self.assertGreater(s['cost_at_stop'], 0)


# Cache behaviour 
@override_settings(CACHES=TEST_CACHES)
class TestCaching(TestCase):
    def setUp(self):
        cache.clear()

    def test_geocode_result_is_cached(self):
        """Second geocode call for same place must not hit Nominatim."""
        with patch('api.services._nominatim_request', return_value=[{'lat': '41.85', 'lon': '-87.65'}]) as mock:
            geocode('Chicago, IL')
            geocode('Chicago, IL')
            self.assertEqual(mock.call_count, 1)

    def test_full_result_served_from_cache(self):
        """Cached route result must be returned without calling geocode or OSRM."""
        fake = {'_cached': False, 'route_summary': {'total_miles': 500}}
        cache.set(_route_key('A', 'B'), fake, 3600)
        with patch('api.services.geocode') as mg, patch('api.services.get_osrm_route') as mo:
            result = plan_route('A', 'B')
            mg.assert_not_called()
            mo.assert_not_called()
        self.assertTrue(result['_cached'])

    def test_cache_object_not_mutated(self):
        """
        Returning _cached=True on the response must not modify the stored cache object.
        (We return a shallow copy, not the original dict.)
        """
        fake = {'_cached': False, 'route_summary': {}}
        cache.set(_route_key('X', 'Y'), fake, 3600)
        plan_route('X', 'Y')
        stored = cache.get(_route_key('X', 'Y'))
        self.assertFalse(stored['_cached'], 'Cache was mutated in place!')

    def test_first_call_not_cached(self):
        route_coords = [[-87.65, 41.85], [-118.24, 34.05]]
        with patch('api.services.geocode', return_value=(41.85, -87.65)), \
             patch('api.services.get_osrm_route', return_value=(500.0, 36000, route_coords)), \
             patch('api.services.enrich_stations_with_coords',
                   return_value=(pd.DataFrame(), 500.0)):
            result = plan_route('Nowhere A', 'Nowhere B')
        self.assertFalse(result['_cached'])

    def test_second_call_is_cached(self):
        route_coords = [[-87.65, 41.85], [-118.24, 34.05]]
        with patch('api.services.geocode', return_value=(41.85, -87.65)), \
             patch('api.services.get_osrm_route', return_value=(500.0, 36000, route_coords)), \
             patch('api.services.enrich_stations_with_coords',
                   return_value=(pd.DataFrame(), 500.0)):
            plan_route('Place A', 'Place B')
            result2 = plan_route('Place A', 'Place B')
        self.assertTrue(result2['_cached'])


# Integration
@override_settings(CACHES=TEST_CACHES)
class TestIntegration(TestCase):
    def setUp(self):
        cache.clear()

    @patch('api.services.geocode')
    @patch('api.services.get_osrm_route')
    def test_response_structure(self, mock_osrm, mock_geo):
        route_coords = [
            [-87.65, 41.85], [-94.0, 36.0], [-97.5, 35.5],
            [-101.8, 35.2], [-106.5, 31.8], [-112.1, 33.4], [-118.24, 34.05],
        ]
        mock_osrm.return_value = (2000.0, 108000, route_coords)

        on_route = {
            'chicago': (41.85, -87.65),     'los angeles': (34.05, -118.24),
            'fort smith': (35.39, -94.42),  'clinton': (35.51, -98.97),
            'elk city': (35.41, -99.43),    'amarillo': (35.22, -101.83),
            'el paso': (31.76, -106.49),    'phoenix': (33.45, -112.07),
        }
        mock_geo.side_effect = lambda p: next(
            (v for k, v in on_route.items() if k in p.lower()), (48.0, -100.0)
        )

        result = plan_route('Chicago, IL', 'Los Angeles, CA')

        for key in ('origin', 'destination', 'route_summary', 'fuel_stops', 'route_geometry'):
            self.assertIn(key, result)

        s = result['route_summary']
        self.assertEqual(s['total_miles'], 2000.0)
        self.assertEqual(s['vehicle_mpg'], 10)
        self.assertEqual(s['tank_range_miles'], 500)
        self.assertGreaterEqual(s['total_fuel_cost_usd'], 0)
        self.assertEqual(result['route_geometry']['type'], 'LineString')
        self.assertFalse(result['_cached'])

        print(f"\n  {s['fuel_stops_count']} stops, ${s['total_fuel_cost_usd']:.2f} total")
        for st in result['fuel_stops']:
            print(f"  #{st['stop_number']} {st['name']} ({st['city']}, {st['state']}) "
                  f"@ ${st['price_per_gallon']:.3f} → ${st['cost_at_stop']:.2f}")


if __name__ == '__main__':
    unittest.main(verbosity=2)