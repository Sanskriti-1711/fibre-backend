"""
Seed a development/admin login so a fresh checkout can authenticate.

``migrate`` creates the ``users_user`` table but no rows, so ``POST
/api/users/login/`` has nothing to match and answers
``400 {"non_field_errors": ["Invalid credentials"]}`` for every credential —
including the ``admin@admin.com`` / ``admin123$`` pair that appears as an
*example* request body in ``docs/apis.md`` and ``docs/admin-user-flow.md``.
Those docs illustrate the request shape; they do not describe a seeded account.

Usage::

    python manage.py seed_dev_admin
    python manage.py seed_dev_admin --email lead@team.com --password 'other-pw'
    python manage.py seed_dev_admin --reset      # update password on existing
    python manage.py seed_dev_admin --force      # allow when DEBUG is False

Idempotent: an existing account is left untouched unless ``--reset`` is passed.

Safety: refuses to run when ``DEBUG`` is False, because this writes a known
password. Pass ``--force`` to override for a throwaway staging database.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

DEFAULT_EMAIL = 'admin@admin.com'
DEFAULT_PASSWORD = 'admin123$'
DEFAULT_FULL_NAME = 'Dev Admin'


class Command(BaseCommand):
    help = 'Create (or reset) a development admin login for POST /api/users/login/.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--email', default=DEFAULT_EMAIL, help=f'Login email (default: {DEFAULT_EMAIL}).'
        )
        parser.add_argument(
            '--password',
            default=DEFAULT_PASSWORD,
            help='Login password (default: the documented example password).',
        )
        parser.add_argument(
            '--name',
            default=DEFAULT_FULL_NAME,
            help=f'Display name (default: {DEFAULT_FULL_NAME}).',
        )
        parser.add_argument(
            '--reset', action='store_true', help='Update the password of an already-seeded account.'
        )
        parser.add_argument(
            '--force', action='store_true', help='Allow seeding even when DEBUG is False.'
        )

    def handle(self, *args, **opts):
        from users.models import User

        if not settings.DEBUG and not opts['force']:
            raise CommandError(
                'Refusing to seed a known password while DEBUG is False. '
                'Use --force if this is a throwaway database.'
            )

        email = User.objects.normalize_email(opts['email']).strip()
        password = opts['password']

        if not email or not password:
            raise CommandError('--email and --password must both be non-empty.')

        existing = User.objects.filter(email__iexact=email).first()
        if existing is not None:
            if not opts['reset']:
                self.stdout.write(
                    self.style.WARNING(
                        f'{existing.email} already exists (role={existing.role}); left unchanged. '
                        'Re-run with --reset to update the password.'
                    )
                )
                return
            existing.set_password(password)
            existing.role = User.Role.SUBADMIN
            existing.is_active = True
            if opts['name']:
                existing.full_name = opts['name']
            existing.save()
            self.stdout.write(self.style.SUCCESS(f'Reset password for {existing.email}.'))
            return

        user = User.objects.create_superuser(
            email=email,
            password=password,
            full_name=opts['name'],
        )
        self.stdout.write(
            self.style.SUCCESS(
                f'Created {user.email} (role={user.role}, superuser={user.is_superuser}).'
            )
        )
        if not settings.DEBUG:
            self.stdout.write(
                self.style.WARNING(
                    'DEBUG is False - treat these credentials as compromised and rotate them.'
                )
            )
        self.stdout.write(f'Log in at POST /api/users/login/ with {email}.')
