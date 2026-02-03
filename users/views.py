from django.shortcuts import get_object_or_404

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from .models import User
from .permissions import IsSubadmin
from .serializers import LoginSerializer, UserCreateSerializer, UserSerializer


class LoginAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]

        refresh = RefreshToken.for_user(user)
        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user": UserSerializer(user).data,
            },
            status=status.HTTP_200_OK,
        )


class UserCreateAPIView(APIView):
    permission_classes = [IsSubadmin]

    def post(self, request):
        serializer = UserCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)


class EngineerListAPIView(APIView):
    permission_classes = [IsSubadmin]

    def get(self, request):
        qs = User.objects.filter(role=User.Role.ENGINEER).order_by("-created_at")
        return Response(UserSerializer(qs, many=True).data, status=status.HTTP_200_OK)


class UserRemoveAPIView(APIView):
    permission_classes = [IsSubadmin]

    def delete(self, request, user_id):
        target = get_object_or_404(User, id=user_id)

        if target.id == request.user.id:
            return Response({"detail": "You cannot delete yourself."}, status=status.HTTP_400_BAD_REQUEST)

        if target.role != User.Role.ENGINEER:
            return Response({"detail": "Only engineers can be removed."}, status=status.HTTP_400_BAD_REQUEST)

        target.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
