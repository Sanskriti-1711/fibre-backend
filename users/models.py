import uuid
from typing import cast

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager):
    def create_user(self, email, password=None, role=None, created_by=None, **extra_fields):
        if not email:
            msg = 'The Email field must be set'
            raise ValueError(msg)

        email = self.normalize_email(email)

        user = cast(
            User,
            self.model(
                email=email,
                role=role or User.Role.ENGINEER,
                created_by=created_by,
                **extra_fields,
            ),
        )
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('is_active', True)

        return self.create_user(
            email=email,
            password=password,
            role=User.Role.SUBADMIN,
            **extra_fields,
        )


class User(AbstractBaseUser, PermissionsMixin):
    class Role(models.TextChoices):
        SUBADMIN = 'SUBADMIN', 'Subadmin'
        ENGINEER = 'ENGINEER', 'Engineer'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=150, blank=True, null=True)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.ENGINEER)

    created_by = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='created_users',
    )

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    def __str__(self):
        return self.email
