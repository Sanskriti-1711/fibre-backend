import os
from pathlib import Path
from datetime import timedelta

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/6.0/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = 'django-insecure-%p=ow#x9d^a8!#t%5p-m0#@(29wyu1f258$ae$a77_jz1rppf0'

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

ALLOWED_HOSTS = ["*"]


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    
    'rest_framework',
    'corsheaders',
    'users',
    'projects',
    'assignments',
    'ftth_hld',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases

# ---------------------------------------------------------------------------
# Database configuration
# ---------------------------------------------------------------------------
# Production: uses two remote PostgreSQL databases (business + GIS) on Zeabur.
# Local dev:   uses a single local Docker PostGIS with gis + business schemas.
#              Switch by setting FTTH_DB=local or uncommenting the local block.
# ---------------------------------------------------------------------------

if os.getenv("FTTH_DB", "").lower() in ("local", "dev", "docker"):
    # Local development — share Docker PostGIS with the FastAPI engine.
    # The Docker postgis service runs on localhost:5432 with database "ftth".
    # Django creates its tables in the "business" schema (search_path order).
    # The FastAPI engine uses the "gis" schema for spatial tables.
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.getenv('PGDATABASE', 'ftth'),
            'USER': os.getenv('PGUSER', 'ftth'),
            'PASSWORD': os.getenv('PGPASSWORD', 'ftth'),
            'HOST': os.getenv('PGHOST', 'localhost'),
            'PORT': os.getenv('PGPORT', '5432'),
            'OPTIONS': {
                'options': '-c search_path=business,public'
            },
        },
    }
else:
    # Production — remote PostgreSQL on Zeabur
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': 'zeabur_db',
            'USER': 'zeabur_user',
            'PASSWORD': 'ybSCV1v2RLdP90xZg73fq456uOhpD8iJ',
            'HOST': '43.157.58.101',
            'PORT': '31768',
        },
        'gis': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': 'fiber_gis',
            'USER': 'zeabur_user',
            'PASSWORD': 'ybSCV1v2RLdP90xZg73fq456uOhpD8iJ',
            'HOST': '43.157.58.101',
            'PORT': '31768',
        },
    }

# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

AUTH_USER_MODEL = 'users.User'

# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

STATIC_URL = 'static/'

# Media files (user uploads)
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# CORS Configuration
CORS_ALLOW_CREDENTIALS = True

# Set CORS_ALLOW_ALL_ORIGINS=true in the environment if you want to allow any origin (dev only).
CORS_ALLOW_ALL_ORIGINS = os.getenv("CORS_ALLOW_ALL_ORIGINS", "false").lower() == "true"

CORS_ALLOWED_ORIGINS = [
    "https://fiberbackend.zeabur.app",
    "https://fiber.zeabur.app",
    "https://fe.dippuzen.com",
    "https://qadmin.dippuzen.com",
    "http://127.0.0.1:5500",
    "http://localhost:5500",
    "http://localhost:8765",
    "http://127.0.0.1:8765",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

CSRF_TRUSTED_ORIGINS = [
    "https://fiberbackend.zeabur.app",
    "https://fe.dippuzen.com",
    "https://qadmin.dippuzen.com",
    "http://127.0.0.1:5500",
    "http://localhost:5500",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

# REST Framework + JWT Config
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(days=7),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=1),
    'AUTH_HEADER_TYPES': ('Bearer',),
}