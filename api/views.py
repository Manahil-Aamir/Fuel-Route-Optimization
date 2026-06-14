import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from .services import plan_route

logger = logging.getLogger(__name__)


class RouteView(APIView):
    """
    POST /api/route/
    {
        "start": "Chicago, IL",
        "end":   "Los Angeles, CA"
    }

    Response includes `_cached: true/false` so callers can observe cache hits.
    """

    def post(self, request):
        start = request.data.get("start", "").strip()
        end   = request.data.get("end",   "").strip()

        if not start or not end:
            return Response(
                {"error": "Both 'start' and 'end' fields are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = plan_route(start, end)
            return Response(result, status=status.HTTP_200_OK)
        except ValueError as exc:
            logger.warning("Route planning error: %s", exc)
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Unexpected error in plan_route")
            return Response(
                {"error": f"Routing failed: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def get(self, request):
        return Response({
            "message": "Fuel Route Optimizer API",
            "usage": {
                "method":   "POST",
                "endpoint": "/api/route/",
                "body":     {"start": "City, State or full address (USA)", "end": "same"},
            },
            "vehicle_assumptions": {"tank_range_miles": 500, "mpg": 10},
            "optimizations": [
                "Result cached (1 hr TTL) : repeated queries served instantly",
                "Geocode results cached (7-day TTL)",
                "OSRM route cached (1-day TTL)",
                "Fuel station CSV loaded once at startup",
                "Parallel city geocoding (6 threads)",
                "Vectorised NumPy haversine for route matching",
                "Bounding-box pre-filter before haversine (~95% skip)",
                "HTTP connection pooling via requests.Session",
            ],
        })
