from pathlib import Path
import os
 
BASE_DIR = Path(__file__).resolve().parent.parent
 
# Security
SECRET_KEY   = os.environ.get('DJANGO_SECRET_KEY', 'dev-only-insecure-key-change-in-prod')
DEBUG        = os.environ.get('DEBUG', 'True') == 'True'
ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', '*').split(',')
 
# Applications 
INSTALLED_APPS = [
    'rest_framework',
    'api.apps.ApiConfig',   # registers AppConfig.ready() to pre-warm the CSV
]
 
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.middleware.common.CommonMiddleware',
]
 
ROOT_URLCONF      = 'config.urls'
WSGI_APPLICATION  = 'config.wsgi.application'
ASGI_APPLICATION  = 'config.asgi.application'
 
# Templates
TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS':    [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
            ],
        },
    },
]
 
# Database 
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME':   ':memory:',
        'TEST':   {'NAME': ':memory:'},
    }
}
 
# Cache 
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
 
CACHES = {
    'default': {
        'BACKEND':  'django_redis.cache.RedisCache',
        'LOCATION': REDIS_URL,
        'OPTIONS': {
            'CLIENT_CLASS':           'django_redis.client.DefaultClient',
            'SOCKET_CONNECT_TIMEOUT':  2,
            'SOCKET_TIMEOUT':          2,
            # If Redis is unreachable, log a warning and recompute
            'IGNORE_EXCEPTIONS':       True,
        },
        'TIMEOUT': 3600,
        'KEY_PREFIX': 'fuelroute',   # avoids key collisions if Redis is shared
    }
}
 
# Cache TTLs
CACHE_TTL_GEOCODE = int(os.environ.get('CACHE_TTL_GEOCODE', 604800))  # 7 days
CACHE_TTL_OSRM    = int(os.environ.get('CACHE_TTL_OSRM',    86400))   # 1 day
CACHE_TTL_ROUTE   = int(os.environ.get('CACHE_TTL_ROUTE',   3600))    # 1 hour
 

# Fuel data
FUEL_DATA_PATH = BASE_DIR / 'fuel_prices.csv'
 

# Django REST Framework
REST_FRAMEWORK = {
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '60/min',   # protects Nominatim/OSRM from being hammered
    },
}
 
# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'simple': {
            'format': '{levelname} {asctime} {module}: {message}',
            'style':  '{',
        },
    },
    'handlers': {
        'console': {
            'class':     'logging.StreamHandler',
            'formatter': 'simple',
        },
    },
    'loggers': {
        'api': {
            'handlers':  ['console'],
            'level':     os.environ.get('LOG_LEVEL', 'DEBUG' if DEBUG else 'INFO'),
            'propagate': False,
        },
    },
}
 