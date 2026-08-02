import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
env = (os.getenv("DJANGO_ENV") or "production").lower()

if env in {"prod", "production"}:
    from .production import *
elif env in {"ci", "test", "testing"}:
    from .development import *
else:
    from .development import *
