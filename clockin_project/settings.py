"""
Django settings for clockin_project.
"""
from pathlib import Path
import os
from decimal import Decimal
from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from .env file
load_dotenv(BASE_DIR / '.env')

# Quick-start development settings - unsuitable for production
SECRET_KEY = os.getenv('SECRET_KEY', 'django-insecure-school-clockin-demo-key-2026-secure!')

DEBUG = os.getenv('DEBUG', 'True').lower() in ('true', '1', 't', 'yes')

raw_hosts = [h.strip() for h in os.getenv('ALLOWED_HOSTS', '*').split(',') if h.strip()]
if '*' not in raw_hosts:
    for internal_host in ('localhost', '127.0.0.1', '0.0.0.0', '[::1]'):
        if internal_host not in raw_hosts:
            raw_hosts.append(internal_host)
ALLOWED_HOSTS = raw_hosts

csrf_trusted = os.getenv('CSRF_TRUSTED_ORIGINS', '')
if csrf_trusted:
    CSRF_TRUSTED_ORIGINS = [origin.strip() for origin in csrf_trusted.split(',') if origin.strip()]

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # App
    'attendance.apps.AttendanceConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'clockin_project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'attendance.context_processors.school_settings',
            ],
        },
    },
]

WSGI_APPLICATION = 'clockin_project.wsgi.application'

# Database Configuration
# Supports both DATABASE_URL (standard for Dokploy/PaaS) and individual DB_* environment variables.
# Falls back to SQLite if USE_SQLITE is True or no PostgreSQL credentials provided.
use_sqlite = os.getenv('USE_SQLITE', 'True').lower() in ('true', '1', 't', 'yes')
database_url = os.getenv('DATABASE_URL')

if database_url:
    use_sqlite = False
    from urllib.parse import urlparse, unquote
    url = urlparse(database_url)
    db_name = url.path[1:]
    db_user = unquote(url.username or 'postgres')
    db_password = unquote(url.password or '')
    db_host = url.hostname or 'localhost'
    db_port = str(url.port or 5432)
else:
    db_name = os.getenv('DB_NAME', 'school_clockin_db')
    db_user = os.getenv('DB_USER', 'postgres')
    db_password = os.getenv('DB_PASSWORD', 'postgres')
    db_host = os.getenv('DB_HOST', 'localhost')
    db_port = os.getenv('DB_PORT', '5432')

if use_sqlite:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': db_name,
            'USER': db_user,
            'PASSWORD': db_password,
            'HOST': db_host,
            'PORT': db_port,
        }
    }

# Password validation
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

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'
WHITENOISE_MANIFEST_STRICT = False

# Production Security & Reverse-Proxy Settings
if not DEBUG:
    SESSION_COOKIE_SECURE = os.getenv('SESSION_COOKIE_SECURE', 'True').lower() in ('true', '1', 't', 'yes')
    CSRF_COOKIE_SECURE = os.getenv('CSRF_COOKIE_SECURE', 'True').lower() in ('true', '1', 't', 'yes')
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = 'SAMEORIGIN'

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Authentication URLs
LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'dashboard'
LOGOUT_REDIRECT_URL = 'login'

# Application specific settings
SCHOOL_NAME = os.getenv('SCHOOL_NAME', 'Geosaka Model School')
CURRENCY_SYMBOL = os.getenv('CURRENCY_SYMBOL', 'GH₵')
DEFAULT_CANTEEN_FEE = Decimal(os.getenv('DEFAULT_CANTEEN_FEE', '10.00'))
CURRENT_ACADEMIC_PERIOD = os.getenv('CURRENT_ACADEMIC_PERIOD', 'Term 1 - 2026')
