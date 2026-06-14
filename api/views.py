import logging
from django.shortcuts import render
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from .services import plan_route

logger = logging.getLogger(__name__)


class RouteView(APIView):
    """
    GET  /api/route/  → renders the browser UI
    POST /api/route/  → returns JSON route plan
    """

    def get(self, request):
        """Serve the interactive browser UI."""
        return render(request, 'api/route.html')

    def post(self, request):
        """
        Plan a fuel-optimised route.

        Request body:
            { "start": "Chicago, IL", "end": "Los Angeles, CA" }

        Response includes _cached: true/false so you can observe cache hits.
        """
        start = request.data.get('start', '').strip()
        end   = request.data.get('end',   '').strip()

        if not start or not end:
            return Response(
                {'error': "Both 'start' and 'end' fields are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = plan_route(start, end)
            return Response(result, status=status.HTTP_200_OK)
        except ValueError as exc:
            logger.warning('Route planning error: %s', exc)
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception('Unexpected error in plan_route')
            return Response(
                {'error': f'Routing failed: {exc}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
            