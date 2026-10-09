from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from config.health import engine_health, healthz

urlpatterns = [
    path('admin/', admin.site.urls),
    # Liveness — the platform's health check points here. Stays DB-free.
    path('healthz', healthz),
    # Readiness — dependency probe for the pipeline engine. Point monitoring
    # here, never the platform's health check. See config/health.py.
    path('healthz/engine', engine_health),
    path('api/users/', include('users.urls')),
    path('api/', include('projects.api.urls')),
    path('api/', include('assignments.api.urls')),
    path('api/', include('ftth_hld.urls')),
    path('api/', include('ftth_lld.urls')),
    path('api/', include('permits.urls')),
    path('api/survey/', include('survey.urls')),
]

# Serve media files in development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
