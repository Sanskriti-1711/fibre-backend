from django.urls import path
from .views import (
    LoginAPIView,
    UserCreateAPIView,
    AdminOnlyAPIView,
    EngineerOnlyAPIView,
)

urlpatterns = [
    path('login/', LoginAPIView.as_view()),
    path('users/', UserCreateAPIView.as_view()),
    path('admin-only/', AdminOnlyAPIView.as_view()),
    path('engineer-only/', EngineerOnlyAPIView.as_view()),
]
