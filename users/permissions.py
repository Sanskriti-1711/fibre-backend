from rest_framework.permissions import BasePermission

from .models import User


class IsSubadmin(BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and getattr(request.user, "role", None) == User.Role.SUBADMIN
        )
