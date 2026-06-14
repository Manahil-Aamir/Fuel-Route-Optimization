from django.urls import path, include
from django.views.generic import RedirectView
 
urlpatterns = [
    # Redirect root → UI
    path('', RedirectView.as_view(url='/api/route/', permanent=False)),
    path('api/', include('api.urls')),
]