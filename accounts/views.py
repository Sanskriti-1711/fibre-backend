from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.contrib.auth import authenticate
from rest_framework_simplejwt.tokens import RefreshToken
from .serializers import LoginSerializer, UserCreateSerializer
from rest_framework.permissions import IsAuthenticated
from .permissions import IsAdmin, IsEngineer

class LoginAPIView(APIView):
    permission_classes = []

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data['email'].lower()

        user = authenticate(
            username=email,
            password=serializer.validated_data['password']
        )

        if not user:
            return Response(
                {"error": "Invalid credentials"},
                status=status.HTTP_401_UNAUTHORIZED
            )

        refresh = RefreshToken.for_user(user)

        return Response({
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "role": user.role,
            "email": user.email,
        })

class UserCreateAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request):
        serializer = UserCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        response_data = {
            "id": user.id,
            "email": user.email,
            "phone": user.phone,
            "role": user.role,
        }

        return Response(response_data, status=status.HTTP_201_CREATED)

class AdminOnlyAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        return Response({"message": "Admin access OK"})


class EngineerOnlyAPIView(APIView):
    permission_classes = [IsAuthenticated, IsEngineer]

    def get(self, request):
        return Response({"message": "Engineer access OK"})