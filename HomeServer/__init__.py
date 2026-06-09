import os
import sys
import logging
from typing import Tuple
from datetime import timedelta
from flask import Flask, redirect, url_for, flash, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO
from flask_wtf import CSRFProtect
from flask_login import LoginManager

from .services import configurations as cfg
from .services.exceptions import AppInitializationError
from .services.database_manager import DatabaseManager

logger = logging.getLogger(__name__)

# Extension Initialization
database = SQLAlchemy()
login_manager = LoginManager()
csrf = CSRFProtect()
socketio: SocketIO = None
db_manager = DatabaseManager(db=database, app_logger=logger)

def initialize_socketio(app: Flask) -> SocketIO:
    """Initializes and configures the Flask-SocketIO instance."""
    global socketio
    async_mode = getattr(cfg, "SOCKETIO_ASYNC_MODE", "eventlet")
    socketio = SocketIO(
        app,
        async_mode=async_mode,
        ping_timeout=cfg.SOCKETIO_PING_TIMEOUT,
        ping_interval=cfg.SOCKETIO_PING_INTERVAL,
        transports=cfg.SOCKETIO_TRANSPORTS,
        max_http_buffer_size=cfg.SOCKETIO_MAX_BUFFER_SIZE,
        cors_allowed_origins=cfg.ALLOWED_ORIGINS if cfg.CORS_ENABLED else None,
        logger=cfg.DEBUG,
        engineio_logger=False,
    )
    logger.info(f"SocketIO initialized with async_mode: {async_mode}")
    return socketio

def create_app() -> Tuple[Flask, SocketIO]:
    """Creates, configures, and returns the Flask application and SocketIO instance."""
    try:
        app = Flask(__name__)
        
        # 1. Load Configuration
        app.config.update(
            SECRET_KEY=cfg.SECRET_KEY,
            SQLALCHEMY_DATABASE_URI=cfg.DATABASE_URL,
            SQLALCHEMY_TRACK_MODIFICATIONS=cfg.SQLALCHEMY_TRACK_MODIFICATIONS,
            SQLALCHEMY_ENGINE_OPTIONS={
                "pool_pre_ping": cfg.POOL_PRE_PING,
                "pool_recycle": cfg.POOL_RECYCLE,
                "pool_size": cfg.POOL_SIZE,
                "max_overflow": cfg.MAX_OVERFLOW,
                "pool_timeout": cfg.POOL_TIMEOUT,
            },
            SESSION_COOKIE_NAME=cfg.SESSION_COOKIE_NAME,
            SESSION_COOKIE_SECURE=cfg.SESSION_COOKIE_SECURE,
            SESSION_COOKIE_HTTPONLY=cfg.SESSION_COOKIE_HTTPONLY,
            SESSION_COOKIE_SAMESITE=cfg.SESSION_COOKIE_SAMESITE,
            PERMANENT_SESSION_LIFETIME=cfg.PERMANENT_SESSION_LIFETIME,
            SESSION_REFRESH_EACH_REQUEST=cfg.SESSION_REFRESH_EACH_REQUEST,
            REMEMBER_COOKIE_DURATION=cfg.REMEMBER_COOKIE_DURATION,
            REMEMBER_COOKIE_SECURE=cfg.REMEMBER_COOKIE_SECURE,
            REMEMBER_COOKIE_HTTPONLY=cfg.REMEMBER_COOKIE_HTTPONLY,
            REMEMBER_COOKIE_SAMESITE=cfg.REMEMBER_COOKIE_SAMESITE,
            PREFERRED_URL_SCHEME=cfg.PREFERRED_URL_SCHEME,
        )

        # 2. Validate Configuration
        if not cfg.SKIP_CONFIG_VALIDATION:
            if not cfg.SECRET_KEY:
                raise AppInitializationError("SECRET_KEY is required")
            if not cfg.DATABASE_URL:
                raise AppInitializationError("DATABASE_URL is required")
            if cfg.SECRET_KEY == "ElectroNora" and cfg.ENV == "production":
                raise AppInitializationError("Default SECRET_KEY must not be used in production")

        # 3. Initialize Core Extensions
        database.init_app(app)
        login_manager.init_app(app)
        csrf.init_app(app)

        if cfg.CORS_ENABLED:
            CORS(
                app,
                origins=cfg.ALLOWED_ORIGINS,
                supports_credentials=cfg.CORS_SUPPORTS_CREDENTIALS,
                methods=cfg.CORS_METHODS,
                allow_headers=cfg.CORS_ALLOWED_HEADERS,
                expose_headers=cfg.CORS_EXPOSE_HEADERS
            )

        # 4. Database Initialization & Model Registration
        with app.app_context():
            try:
                from . import models 
                logger.info("Models registered successfully")
            except ImportError as e:
                logger.warning(f"Could not import models package: {e}")
            
            if cfg.AUTO_CREATE_DB:
                if not db_manager.initialize_schema():
                    raise AppInitializationError("Database schema initialization failed.")

        # 5. Initialize SocketIO
        sio = initialize_socketio(app)
        app.extensions['socketio'] = sio
        
        # 6. Register Blueprints
        try:
            from .routes.auth.Wizard import wizard
            app.register_blueprint(wizard, url_prefix='/setup')
        
            from .routes.auth.login import auth_bp, main_bp, api_bp
            app.register_blueprint(auth_bp, url_prefix='/')
            app.register_blueprint(main_bp, url_prefix='/')
            app.register_blueprint(api_bp, url_prefix='/api')

            from .routes.users.MessageApi import message_api_bp
            app.register_blueprint(message_api_bp, url_prefix='/chat')
            logger.info("Blueprints registered successfully")

        except ImportError as e:
            logger.warning(f"Could not import blueprints: {e}")

        # register SocketIO namespaces
        try:
            from .services.login_socketio import AuthNamespace
            sio.on_namespace(AuthNamespace('/auth'))

            from.services.Chat_socketio import message_socket_events
            message_socket_events(sio)
            logger.info("SocketIO namespaces registered successfully")
        except ImportError as e:
            logger.warning(f"Could not import SocketIO namespaces: {e}")

        # 7. Register Hooks
        @app.before_request
        def log_request():
            if not request.path.startswith('/static/'):
                logger.debug(f"Request: {request.method} {request.path}")

        @app.before_request
        def enforce_setup_wizard():
            """Redirect ALL traffic to the setup wizard if no users exist."""
            SETUP_EXEMPT = {'/setup', '/setup/', '/health', '/api/health'}
            
            # Only allow static files and explicit setup/health endpoints to bypass
            if request.path.startswith('/static/') or request.path in SETUP_EXEMPT:
                return None

            try:
                from .models import User
                if User.query.count() == 0:
                    return redirect(url_for('wizard.setup_wizard'))
            except Exception as e:
                logger.warning(f"Setup wizard check failed (DB might be empty or down): {e}")

        # 8. Configure Login Manager
        try:
            from .models import User
            
            @login_manager.user_loader
            def load_user(user_id: str):
                try:
                    return User.query.get(int(user_id))
                except (ValueError, TypeError):
                    return None

            login_manager.login_view = 'auth.login'
            login_manager.login_message = 'Please log in to access this page.'
            login_manager.login_message_category = 'info'
            login_manager.refresh_view = 'auth.login'
            login_manager.needs_refresh_message = 'Session expired, please re-login'
            login_manager.needs_refresh_message_category = 'warning'
 
            @login_manager.unauthorized_handler
            def unauthorized():
                if request.path.startswith('/api/'):
                    return jsonify({'error': 'Authentication required'}), 401
                flash('Please log in to access this page.', 'warning')
                return redirect(url_for('auth.login', next=request.path))
        except ImportError:
            logger.warning("User model not found, skipping Login Manager configuration.")

        # 9. Initialize Scheduler and Admin Panel
        # try:
        #     from .scheduler import init_scheduler 
        #     init_scheduler(app)
        # except ImportError:
        #     pass

        try:
            from HomeServer.routes.admin.admin import init_admin
            init_admin(app, database)
        except ImportError as e:
            app.logger.warning(f"Flask-Admin not available: {e}")
        except Exception as e:
            app.logger.error(f"Flask-Admin init failed: {e}")
           
           
        logger.info("Application initialized successfully")
        return app, sio

    except AppInitializationError:
        raise
    except Exception as error:
        logger.exception("Application initialization failed")
        raise AppInitializationError(f"Application creation failed: {error}") from error