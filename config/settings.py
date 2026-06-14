from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'django-insecure-fuel-route-demo-key-change-in-production')
DEBUG = os.environ.get('DEBUG', 'True') == 'True'
ALLOWED_HOSTS = ['*']

INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.auth',
    'rest_framework',
    'api',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.middleware.common.CommonMiddleware',
]

ROOT_URLCONF = 'config.urls'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

FUEL_DATA_PATH = BASE_DIR / 'fuel_prices.csv'

# ---------------------------------------------------------------------------
# Cache — tries Redis first, falls back to local-memory (works out of the box)
# ---------------------------------------------------------------------------
REDIS_URL = os.environ.get('REDIS_URL', '')

if REDIS_URL:
    CACHES = {
        'default': {
            'BACKEND': 'django_redis.cache.RedisCache',
            'LOCATION': REDIS_URL,
            'OPTIONS': {
                'CLIENT_CLASS': 'django_redis.client.DefaultClient',
                'SOCKET_CONNECT_TIMEOUT': 2,
                'SOCKET_TIMEOUT': 2,
                'IGNORE_EXCEPTIONS': True,   # fall through to re-compute if Redis is down
            },
            'TIMEOUT': 3600,                 # 1 hour default TTL
        }
    }
else:
    # Zero-config fallback: per-process in-memory cache
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'TIMEOUT': 3600,
        }
    }

# Cache TTLs (seconds)
CACHE_TTL_GEOCODE = 86400 * 7   # city coords don't change – 7 days
CACHE_TTL_OSRM    = 86400       # route geometry – 1 day
CACHE_TTL_ROUTE   = 3600        # full plan result – 1 hour (fuel prices change)

REST_FRAMEWORK = {
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
}
