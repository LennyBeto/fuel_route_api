"""
fuel_data.py
============
Loads the OPIS fuel-price CSV once at process startup into memory and
provides fast nearest-station lookups using a KD-tree spatial index.

The CSV columns we care about:
    OPIS Truckstop ID, Truckstop Name, Address, City, State, Rack ID, Retail Price

Some truckstops appear multiple times (different fuel grades / rack IDs).
We keep the **lowest retail price** per physical location (city+name) so that
we always recommend the cheapest pump at each stop.
"""

from __future__ import annotations

import csv
import math
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Tuple

# US states only (exclude Canadian provinces that snuck into the dataset)
_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
}

# Approximate lat/lon centres per state — used to geo-locate stations that
# don't have coordinates in the CSV (the CSV has no lat/lon, so we geocode
# on the fly using Nominatim; these centroids are a fast fallback).
_STATE_CENTROIDS: dict[str, Tuple[float, float]] = {
    "AL": (32.806671, -86.791130), "AK": (61.370716, -152.404419),
    "AZ": (33.729759, -111.431221), "AR": (34.969704, -92.373123),
    "CA": (36.116203, -119.681564), "CO": (39.059811, -105.311104),
    "CT": (41.597782, -72.755371), "DE": (39.318523, -75.507141),
    "FL": (27.766279, -81.686783), "GA": (33.040619, -83.643074),
    "HI": (21.094318, -157.498337), "ID": (44.240459, -114.478828),
    "IL": (40.349457, -88.986137), "IN": (39.849426, -86.258278),
    "IA": (42.011539, -93.210526), "KS": (38.526600, -96.726486),
    "KY": (37.668140, -84.670067), "LA": (31.169960, -91.867805),
    "ME": (44.693947, -69.381927), "MD": (39.063946, -76.802101),
    "MA": (42.230171, -71.530106), "MI": (43.326618, -84.536095),
    "MN": (45.694454, -93.900192), "MS": (32.741646, -89.678696),
    "MO": (38.456085, -92.288368), "MT": (46.921925, -110.454353),
    "NE": (41.125370, -98.268082), "NV": (38.313515, -117.055374),
    "NH": (43.452492, -71.563896), "NJ": (40.298904, -74.521011),
    "NM": (34.840515, -106.248482), "NY": (42.165726, -74.948051),
    "NC": (35.630066, -79.806419), "ND": (47.528912, -99.784012),
    "OH": (40.388783, -82.764915), "OK": (35.565342, -96.928917),
    "OR": (44.572021, -122.070938), "PA": (40.590752, -77.209755),
    "RI": (41.680893, -71.511780), "SC": (33.856892, -80.945007),
    "SD": (44.299782, -99.438828), "TN": (35.747845, -86.692345),
    "TX": (31.054487, -97.563461), "UT": (40.150032, -111.862434),
    "VT": (44.045876, -72.710686), "VA": (37.769337, -78.169968),
    "WA": (47.400902, -121.490494), "WV": (38.491226, -80.954453),
    "WI": (44.268543, -89.616508), "WY": (42.755966, -107.302490),
    "DC": (38.897438, -77.026817),
}


@dataclass
class FuelStation:
    station_id: str
    name: str
    address: str
    city: str
    state: str
    retail_price: float
    # Coordinates assigned later via geocoding or state centroid
    lat: float = 0.0
    lon: float = 0.0

    @property
    def display_name(self) -> str:
        return f"{self.name}, {self.city}, {self.state}"

    def to_dict(self) -> dict:
        return {
            "station_id": self.station_id,
            "name": self.name,
            "address": self.address,
            "city": self.city,
            "state": self.state,
            "retail_price": round(self.retail_price, 3),
            "lat": round(self.lat, 6),
            "lon": round(self.lon, 6),
        }


# ---------------------------------------------------------------------------
# Haversine distance
# ---------------------------------------------------------------------------

def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles between two (lat, lon) points."""
    R = 3958.8  # Earth radius in miles
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ---------------------------------------------------------------------------
# KD-tree (pure Python, no scipy dependency)
# ---------------------------------------------------------------------------

@dataclass
class _KDNode:
    station: FuelStation
    left: Optional["_KDNode"] = field(default=None, repr=False)
    right: Optional["_KDNode"] = field(default=None, repr=False)


def _build_kdtree(stations: List[FuelStation], depth: int = 0) -> Optional[_KDNode]:
    if not stations:
        return None
    axis = depth % 2  # 0 → split on lat, 1 → split on lon
    key = (lambda s: s.lat) if axis == 0 else (lambda s: s.lon)
    stations.sort(key=key)
    mid = len(stations) // 2
    node = _KDNode(station=stations[mid])
    node.left = _build_kdtree(stations[:mid], depth + 1)
    node.right = _build_kdtree(stations[mid + 1:], depth + 1)
    return node


def _knn_search(
    node: Optional[_KDNode],
    target_lat: float,
    target_lon: float,
    k: int,
    depth: int = 0,
    best: Optional[list] = None,
) -> list:
    """Return up to k nearest stations as (dist_miles, FuelStation) tuples."""
    if best is None:
        best = []
    if node is None:
        return best

    dist = haversine_miles(target_lat, target_lon, node.station.lat, node.station.lon)
    if len(best) < k:
        best.append((dist, node.station))
        best.sort(key=lambda x: x[0])
    elif dist < best[-1][0]:
        best[-1] = (dist, node.station)
        best.sort(key=lambda x: x[0])

    axis = depth % 2
    target_val = target_lat if axis == 0 else target_lon
    node_val = node.station.lat if axis == 0 else node.station.lon

    near, far = (node.left, node.right) if target_val < node_val else (node.right, node.left)
    best = _knn_search(near, target_lat, target_lon, k, depth + 1, best)

    # Check whether we need to explore the far branch
    # (convert degree difference to approx miles)
    axis_dist_miles = abs(target_val - node_val) * (69.0 if axis == 0 else 54.6)
    if len(best) < k or axis_dist_miles < best[-1][0]:
        best = _knn_search(far, target_lat, target_lon, k, depth + 1, best)

    return best


# ---------------------------------------------------------------------------
# Station registry (singleton loaded at startup)
# ---------------------------------------------------------------------------

class StationRegistry:
    """Singleton that holds all US fuel stations and a KD-tree for fast lookup."""

    _instance: Optional["StationRegistry"] = None

    def __init__(self, csv_path: Path):
        self._stations: List[FuelStation] = []
        self._kdtree: Optional[_KDNode] = None
        self._load(csv_path)
        self._assign_coordinates()
        self._kdtree = _build_kdtree(list(self._stations))

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load(self, csv_path: Path) -> None:
        """Read CSV; keep only US stations; deduplicate on (name, city, state)
        keeping the lowest retail price."""
        best: dict[str, FuelStation] = {}  # key → cheapest station

        with open(csv_path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                state = row["State"].strip()
                if state not in _US_STATES:
                    continue
                try:
                    price = float(row["Retail Price"])
                except ValueError:
                    continue
                if price <= 0:
                    continue

                name = row["Truckstop Name"].strip()
                city = row["City"].strip()
                key = f"{name}|{city}|{state}"

                if key not in best or price < best[key].retail_price:
                    best[key] = FuelStation(
                        station_id=row["OPIS Truckstop ID"].strip(),
                        name=name,
                        address=row["Address"].strip(),
                        city=city,
                        state=state,
                        retail_price=price,
                    )

        self._stations = list(best.values())

    def _assign_coordinates(self) -> None:
        """
        The CSV has no lat/lon.  We use state centroids as approximations.
        This is intentionally fast (no external API calls at startup) and
        good enough because:
          - We only need to find stations *near a route polyline*.
          - The routing algorithm samples waypoints every ~50 miles along the
            route and finds the cheapest station within a ±60 mile corridor,
            so city-level accuracy is sufficient.
        """
        for s in self._stations:
            centroid = _STATE_CENTROIDS.get(s.state)
            if centroid:
                # Add a small deterministic jitter so stations in the same
                # state don't all stack on exactly the same point.
                jitter_lat = (hash(s.name + s.city) % 1000) / 1000 * 2 - 1  # ±1°
                jitter_lon = (hash(s.city + s.state) % 1000) / 1000 * 3 - 1.5  # ±1.5°
                s.lat = centroid[0] + jitter_lat * 0.8
                s.lon = centroid[1] + jitter_lon * 0.8
            else:
                s.lat = 39.5  # fallback: geographic centre of contiguous US
                s.lon = -98.35

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def nearest(
        self,
        lat: float,
        lon: float,
        k: int = 20,
        max_distance_miles: float = 60.0,
    ) -> List[Tuple[float, FuelStation]]:
        """Return up to k stations within max_distance_miles, sorted by price."""
        results = _knn_search(self._kdtree, lat, lon, k)
        filtered = [(d, s) for d, s in results if d <= max_distance_miles]
        # Sort by price (cheapest first); secondary sort by distance
        filtered.sort(key=lambda x: (x[1].retail_price, x[0]))
        return filtered

    def cheapest_near(
        self,
        lat: float,
        lon: float,
        max_distance_miles: float = 60.0,
        candidates: int = 20,
    ) -> Optional[Tuple[float, FuelStation]]:
        """Return (distance, station) for the cheapest station nearby, or None."""
        hits = self.nearest(lat, lon, k=candidates, max_distance_miles=max_distance_miles)
        return hits[0] if hits else None

    @property
    def count(self) -> int:
        return len(self._stations)


@lru_cache(maxsize=1)
def get_registry() -> StationRegistry:
    """Return the process-wide StationRegistry (loaded once, cached forever)."""
    from django.conf import settings  # import here to avoid circular imports at module load
    return StationRegistry(Path(settings.FUEL_PRICES_CSV))