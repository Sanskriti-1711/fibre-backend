from django.urls import path
from .views import LoginAPIView, UserCreateAPIView

urlpatterns = [
    path('login/', LoginAPIView.as_view()),
    path('users/', UserCreateAPIView.as_view()),
]
