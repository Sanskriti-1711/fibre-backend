from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from .models import User
from .permissions import IsSubadmin
from .serializers import LoginSerializer, UserCreateSerializer, UserSerializer


class LoginAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data['user']

        refresh = RefreshToken.for_user(user)
        return Response(
            {
                'access': str(refresh.access_token),
                'refresh': str(refresh),
                'user': UserSerializer(user).data,
            },
            status=status.HTTP_200_OK,
        )


class UserCreateAPIView(APIView):
    permission_classes = [IsSubadmin]

    def post(self, request):
        serializer = UserCreateSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)


class EngineerListAPIView(APIView):
    permission_classes = [IsSubadmin]

    def get(self, request):
        qs = User.objects.filter(role=User.Role.ENGINEER).order_by('-created_at')
        return Response(UserSerializer(qs, many=True).data, status=status.HTTP_200_OK)


class UserListAPIView(APIView):
    """List all active users (subadmins + engineers) so project members
    can be assigned to any role (planner / engineer / reviewer / contractor)."""

    permission_classes = [IsSubadmin]

    def get(self, request):
        qs = User.objects.filter(is_active=True).order_by('role', '-created_at')
        return Response(UserSerializer(qs, many=True).data, status=status.HTTP_200_OK)


class UserRemoveAPIView(APIView):
    permission_classes = [IsSubadmin]

    def delete(self, request, user_id):
        target = get_object_or_404(User, id=user_id)

        if target.id == request.user.id:
            return Response(
                {'detail': 'You cannot delete yourself.'}, status=status.HTTP_400_BAD_REQUEST
            )

        if target.role != User.Role.ENGINEER:
            return Response(
                {'detail': 'Only engineers can be removed.'}, status=status.HTTP_400_BAD_REQUEST
            )

        target.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class LogoutAPIView(APIView):
    """POST /api/users/logout/ — withdraw the caller's refresh token.

    Until this route existed a session could not be ended: the tokens stayed
    valid for their full lifetime, so a lost field phone kept minting 12-hour
    access tokens for a month and support had no lever to pull. The client
    posts the refresh token it holds; the access token expires on its own.
    """

    def post(self, request):
        refresh = (request.data or {}).get('refresh')
        if not refresh:
            return Response({'detail': 'refresh is required'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            RefreshToken(refresh).blacklist()
        except TokenError:
            # Already expired, already blacklisted, or not a token at all:
            # either way the caller ends up signed out, which is the point.
            return Response(status=status.HTTP_204_NO_CONTENT)
        return Response(status=status.HTTP_204_NO_CONTENT)


class PasswordChangeAPIView(APIView):
    """POST /api/users/password/change/ — set a new password for the caller.

    Requires the current password (a stolen access token is not enough), runs
    Django's configured validators, and retires every refresh token the user
    holds so a session opened with the old password cannot be renewed.
    """

    def post(self, request):
        old_password = (request.data or {}).get('old_password') or ''
        new_password = (request.data or {}).get('new_password') or ''

        if not request.user.check_password(old_password):
            return Response(
                {'detail': 'Current password is incorrect.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            validate_password(new_password, request.user)
        except ValidationError as exc:
            return Response({'detail': exc.messages}, status=status.HTTP_400_BAD_REQUEST)

        request.user.set_password(new_password)
        request.user.save(update_fields=['password'])

        # The password just changed, so every refresh token issued before this
        # moment must stop working; the outstanding access tokens expire within
        # ACCESS_TOKEN_LIFETIME.
        OutstandingToken.objects.filter(user=request.user).delete()

        return Response(status=status.HTTP_204_NO_CONTENT)
