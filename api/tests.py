"""
tests.py
========
Unit tests for the Fuel Route API.

Run with:
    python manage.py test api
"""

from unittest.mock import MagicMock, patch
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status

from .fuel_data import haversine_miles, FuelStation, StationRegistry, _build_kdtree, _knn_search
from .routing import _decode_polyline, _sample_waypoints, _mile_to_polyline_index, _cumulative_distances


# ---------------------------------------------------------------------------
# fuel_data tests
# ---------------------------------------------------------------------------

class HaversineTestCase(TestCase):
    def test_same_point(self):
        self.assertAlmostEqual(haversine_miles(40.0, -74.0, 40.0, -74.0), 0.0, places=3)

    def test_known_distance(self):
        # NYC to LA is ~2445 miles
        dist = haversine_miles(40.7128, -74.0060, 34.0522, -118.2437)
        self.assertGreater(dist, 2400)
        self.assertLess(dist, 2500)

    def test_short_distance(self):
        # ~69 miles per degree latitude
        dist = haversine_miles(40.0, -74.0, 41.0, -74.0)
        self.assertAlmostEqual(dist, 69.0, delta=2.0)


class KDTreeTestCase(TestCase):
    def _make_station(self, lat, lon, price=3.0, name="S"):
        s = FuelStation(
            station_id="1", name=name, address="", city="City",
            state="TX", retail_price=price
        )
        s.lat = lat
        s.lon = lon
        return s

    def test_nearest_finds_closest(self):
        stations = [
            self._make_station(30.0, -97.0, name="A"),
            self._make_station(31.0, -97.0, name="B"),
            self._make_station(32.0, -97.0, name="C"),
        ]
        tree = _build_kdtree(stations)
        results = _knn_search(tree, 30.1, -97.0, k=1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][1].name, "A")

    def test_knn_returns_k_results(self):
        stations = [self._make_station(30.0 + i * 0.5, -97.0, name=str(i)) for i in range(10)]
        tree = _build_kdtree(stations)
        results = _knn_search(tree, 32.0, -97.0, k=3)
        self.assertEqual(len(results), 3)

    def test_empty_tree(self):
        results = _knn_search(None, 30.0, -97.0, k=5)
        self.assertEqual(results, [])


# ---------------------------------------------------------------------------
# routing tests
# ---------------------------------------------------------------------------

class DecodePolylineTestCase(TestCase):
    def test_decode_known_polyline(self):
        # Encoding for a simple two-point line (approx)
        # We test round-trip: encode a known point and decode it.
        # Using the well-known encoded polyline for [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]
        encoded = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
        pts = _decode_polyline(encoded)
        self.assertEqual(len(pts), 3)
        self.assertAlmostEqual(pts[0][0], 38.5, places=1)
        self.assertAlmostEqual(pts[0][1], -120.2, places=1)

    def test_single_point(self):
        # Encode (0,0) manually: both values are 0 → encoded as '?'
        pts = _decode_polyline("??")
        self.assertEqual(len(pts), 1)
        self.assertAlmostEqual(pts[0][0], 0.0, places=4)
        self.assertAlmostEqual(pts[0][1], 0.0, places=4)


class SampleWaypointsTestCase(TestCase):
    def test_returns_start_and_end(self):
        polyline = [(i * 0.1, -97.0) for i in range(100)]
        waypoints = _sample_waypoints(polyline, interval_miles=50.0)
        self.assertEqual(waypoints[0], polyline[0])
        self.assertEqual(waypoints[-1], polyline[-1])

    def test_single_point_polyline(self):
        polyline = [(30.0, -97.0)]
        waypoints = _sample_waypoints(polyline, interval_miles=50.0)
        self.assertEqual(len(waypoints), 1)

    def test_empty_polyline(self):
        waypoints = _sample_waypoints([], interval_miles=50.0)
        self.assertEqual(waypoints, [])


class CumulativeDistancesTestCase(TestCase):
    def test_start_is_zero(self):
        poly = [(30.0, -97.0), (31.0, -97.0)]
        cum = _cumulative_distances(poly)
        self.assertEqual(cum[0], 0.0)

    def test_increases_monotonically(self):
        poly = [(30.0 + i * 0.5, -97.0) for i in range(5)]
        cum = _cumulative_distances(poly)
        for i in range(1, len(cum)):
            self.assertGreater(cum[i], cum[i - 1])


class MileToIndexTestCase(TestCase):
    def test_zero_mile(self):
        cum = [0.0, 50.0, 100.0, 150.0]
        self.assertEqual(_mile_to_polyline_index(cum, 0.0), 0)

    def test_exact_mile(self):
        cum = [0.0, 50.0, 100.0, 150.0]
        self.assertEqual(_mile_to_polyline_index(cum, 100.0), 2)

    def test_beyond_end(self):
        cum = [0.0, 50.0, 100.0]
        idx = _mile_to_polyline_index(cum, 200.0)
        self.assertEqual(idx, len(cum) - 1)


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------

class HealthEndpointTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_health_ok(self):
        with patch("api.views.get_registry") as mock_reg:
            mock_reg.return_value.count = 4821
            resp = self.client.get("/api/health/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["status"], "ok")
        self.assertEqual(resp.data["fuel_stations_loaded"], 4821)


class StationsInfoEndpointTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_stations_info(self):
        with patch("api.views.get_registry") as mock_reg:
            mock_reg.return_value.count = 4821
            resp = self.client.get("/api/stations/info/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn("total_us_stations", resp.data)


class RouteEndpointValidationTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_missing_start(self):
        resp = self.client.post("/api/route/", {"end": "Chicago, IL"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_missing_end(self):
        resp = self.client.post("/api/route/", {"start": "Dallas, TX"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_same_start_end(self):
        resp = self.client.post(
            "/api/route/",
            {"start": "Dallas, TX", "end": "Dallas, TX"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_mpg(self):
        resp = self.client.post(
            "/api/route/",
            {"start": "Dallas, TX", "end": "Chicago, IL", "mpg": -5},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_range(self):
        resp = self.client.post(
            "/api/route/",
            {"start": "Dallas, TX", "end": "Chicago, IL", "vehicle_range_miles": 10},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class RouteEndpointSuccessTestCase(TestCase):
    """Mock external calls to test the full response shape."""

    def setUp(self):
        self.client = APIClient()

    def _mock_response(self):
        """Build a realistic mocked build_route_response return value."""
        return {
            "origin": {"query": "Dallas, TX", "lat": 32.78, "lon": -96.80},
            "destination": {"query": "Chicago, IL", "lat": 41.88, "lon": -87.63},
            "route": {
                "distance_miles": 921.0,
                "estimated_duration_hours": 13.5,
                "polyline_encoded": "_someEncodedString",
                "polyline": [{"lat": 32.78, "lon": -96.80}, {"lat": 41.88, "lon": -87.63}],
            },
            "vehicle": {"range_miles": 500, "mpg": 10},
            "fuel_stops": [
                {
                    "stop_number": 1,
                    "distance_from_start_miles": 450.0,
                    "station": {
                        "station_id": "99",
                        "name": "CHEAP FUEL STOP",
                        "address": "I-44, EXIT 100",
                        "city": "Springfield",
                        "state": "MO",
                        "retail_price": 2.999,
                        "lat": 37.22,
                        "lon": -93.29,
                    },
                    "detour_miles": 0.5,
                    "gallons_purchased": 45.0,
                    "cost_usd": 134.96,
                    "price_per_gallon": 2.999,
                }
            ],
            "summary": {
                "num_fuel_stops": 1,
                "total_gallons_needed": 92.1,
                "total_fuel_cost_usd": 275.0,
                "avg_price_per_gallon": 2.987,
            },
            "_meta": {"processing_time_ms": 312},
        }

    def test_successful_route(self):
        with patch("api.views.build_route_response", return_value=self._mock_response()):
            resp = self.client.post(
                "/api/route/",
                {"start": "Dallas, TX", "end": "Chicago, IL"},
                format="json",
            )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data
        self.assertIn("origin", data)
        self.assertIn("destination", data)
        self.assertIn("route", data)
        self.assertIn("fuel_stops", data)
        self.assertIn("summary", data)
        self.assertEqual(data["summary"]["num_fuel_stops"], 1)

    def test_geocode_error_returns_422(self):
        with patch("api.views.build_route_response", side_effect=ValueError("Could not geocode")):
            resp = self.client.post(
                "/api/route/",
                {"start": "Fakecity, XX", "end": "Chicago, IL"},
                format="json",
            )
        self.assertEqual(resp.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertIn("error", resp.data)

    def test_connection_error_returns_503(self):
        import requests as req_lib
        with patch("api.views.build_route_response", side_effect=req_lib.exceptions.ConnectionError()):
            resp = self.client.post(
                "/api/route/",
                {"start": "Dallas, TX", "end": "Chicago, IL"},
                format="json",
            )
        self.assertEqual(resp.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_timeout_returns_504(self):
        import requests as req_lib
        with patch("api.views.build_route_response", side_effect=req_lib.exceptions.Timeout()):
            resp = self.client.post(
                "/api/route/",
                {"start": "Dallas, TX", "end": "Chicago, IL"},
                format="json",
            )
        self.assertEqual(resp.status_code, status.HTTP_504_GATEWAY_TIMEOUT)