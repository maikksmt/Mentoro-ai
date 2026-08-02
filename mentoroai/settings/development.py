import os

from .base import *
from .base import DATABASES, INSTALLED_APPS

DEBUG = True

DJANGO_LOG_LEVEL = os.getenv("DJANGO_LOG_LEVEL", "DEBUG")

SECRET_KEY = "dev-unsafe-change-me"

ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]

INSTALLED_APPS += [
    "rosetta",
]

# E-Mail
# EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Rosetta
ROSETTA_MESSAGES_PER_PAGE = 20

# mentoroai/settings/development.py  (nur für TESTS)
DATABASES["default"]["TEST"] = {"NAME": "test_mentoroai"}
DATABASES['default']['CONN_MAX_AGE'] = 0
