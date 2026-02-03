from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from .views import EngineerListAPIView, LoginAPIView, UserCreateAPIView, UserRemoveAPIView


urlpatterns = [
    path("login/", LoginAPIView.as_view(), name="user-login"),
    path("", UserCreateAPIView.as_view(), name="user-create"),
    path("engineers/", EngineerListAPIView.as_view(), name="engineer-list"),
    path("<uuid:user_id>/", UserRemoveAPIView.as_view(), name="user-remove"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
]
