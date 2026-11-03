from django.contrib import admin
from django.urls import include, path, re_path

from config.health import engine_health, healthz
from config.media import serve_media

urlpatterns = [
    path('admin/', admin.site.urls),
    # Liveness — the platform's health check points here. Stays DB-free.
    path('healthz', healthz),
    # Readiness — dependency probe for the pipeline engine. Point monitoring
    # here, never the platform's health check. See config/health.py.
    path('healthz/engine', engine_health),
    path('api/users/', include('users.urls')),
    path('api/', include('projects.api.urls')),
    path('api/', include('supervision.urls')),
    path('api/', include('assignments.api.urls')),
    path('api/', include('ftth_hld.urls')),
    path('api/', include('ftth_lld.urls')),
    path('api/', include('permits.urls')),
    path('api/survey/', include('survey.urls')),
    path('api/', include('notifications.urls')),
    # Uploaded evidence, in every environment. Django's `static()` helper only
    # mounts MEDIA_URL when DEBUG is on, which is why every photo URL 404'd on
    # the deployed host. Mounted unconditionally so development and the host
    # answer from the same code — see config/media.py.
    re_path(r'^media/(?P<path>.*)$', serve_media, name='serve-media'),
]
