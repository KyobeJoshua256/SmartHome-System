import os
import secrets
import logging
from datetime import timedelta

# ==============================================================================
# 1. CORE APPLICATION SETTINGS
# ==============================================================================
APP_NAME = "ElectroNora"
VERSION = "5.0.0"
ENV = os.getenv("FLASK_ENV", "development")
DEBUG = ENV == "development"
SKIP_CONFIG_VALIDATION = False

_secret_key_default = secrets.token_hex(32) if ENV != "production" else ""
SECRET_KEY = os.getenv("SECRET_KEY", _secret_key_default)

# ==============================================================================
# 2. SECURITY & SESSION CONFIGURATION
# ==============================================================================
SESSION_COOKIE_NAME = "smart_home_session"
SESSION_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_REFRESH_EACH_REQUEST = False
PERMANENT_SESSION_LIFETIME = timedelta(days=30)

REMEMBER_COOKIE_DURATION = timedelta(days=30)
REMEMBER_COOKIE_SECURE = not DEBUG
REMEMBER_COOKIE_HTTPONLY = True
REMEMBER_COOKIE_SAMESITE = "Lax"

PREFERRED_URL_SCHEME = 'https' if not DEBUG else 'http'
WTF_CSRF_TIME_LIMIT = 3600


# ==============================================================================
# 3. DATABASE CONFIGURATION
# ==============================================================================
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "ElectroNora.sqlite3")
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DEFAULT_DB_PATH}")

SQLALCHEMY_TRACK_MODIFICATIONS = False
POOL_PRE_PING = True
POOL_RECYCLE = 300
POOL_SIZE = 10
MAX_OVERFLOW = 20
POOL_TIMEOUT = 30
AUTO_CREATE_DB = True


# ==============================================================================
# 4. SOCKETIO CONFIGURATION
# ==============================================================================
SOCKETIO_ASYNC_MODE = "gevent"
SOCKETIO_PING_TIMEOUT = 120
SOCKETIO_PING_INTERVAL = 25
SOCKETIO_TRANSPORTS = ["websocket", "polling"]
SOCKETIO_MAX_BUFFER_SIZE = 10 * 1024 * 1024

# ==============================================================================
# 5. CORS & NETWORK CONFIGURATION
# ==============================================================================
HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "5000"))

ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*")
CORS_ENABLED = True
CORS_SUPPORTS_CREDENTIALS = True
CORS_METHODS = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
CORS_ALLOWED_HEADERS = ["Content-Type", "Authorization", "X-CSRF-Token"]
CORS_EXPOSE_HEADERS = ["Content-Type", "X-CSRF-Token"]

# ==============================================================================
# 6. LOGGING CONFIGURATION
# ==============================================================================
LOG_LEVEL = logging.INFO
LOG_FILE = os.getenv("LOG_FILE", "electro_nora.log")

# ==============================================================================
# 7. FEATURE FLAGS
# ==============================================================================
ENABLE_API = True
ENABLE_SOCKETIO = True
ENABLE_ADMIN_PANEL = True
ENABLE_USER_REGISTRATION = True
ENABLE_FINGERPRINT_AUTH = True
ENABLE_GUEST_ACCESS = True
ENABLE_SMS_AUTH = True

# ==============================================================================
# 8. INFRASTRUCTURE & DEPLOYMENT
# ==============================================================================
USE_ASGI = False
ALLOW_UNSAFE_WERKZEUG = True
WORKER_COUNT = 1
AUTO_RELOAD = True
SHOW_STARTUP_BANNER = True
PROFILING_ENABLED = False

# ==============================================================================
# 9. EXTERNAL SERVICES (SMS, EMAIL)
# ==============================================================================
SMS_PROVIDER = os.getenv("SMS_PROVIDER", "console")
EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "console")
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "noreply@electro-nora.local")