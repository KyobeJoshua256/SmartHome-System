import os
import sys
import logging
from sqlalchemy import text, inspect
from sqlalchemy.exc import OperationalError
from flask import Flask
from flask_sqlalchemy import SQLAlchemy

# =============================================================================
# SECTION 1: Logging Configuration
# =============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# =============================================================================
# SECTION 2: Database Manager Class
# =============================================================================
class DatabaseManager:
    """
    Manages database connectivity and safe schema initialization.
    
    Note: In production, schema changes should be handled via CI/CD 
    using 'flask db upgrade'. This class provides connectivity checks 
    and a safe fallback for initial local development.
    """
    
    def __init__(self, db: SQLAlchemy, app_logger: logging.Logger = None):
        self.db = db
        self.logger = app_logger or logger

    def check_connection(self) -> bool:
        """Verifies if the database is reachable and responsive."""
        try:
            with self.db.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            self.logger.info("Database connection established successfully.")
            return True
        except OperationalError as e:
            self.logger.error(f"Failed to connect to database: {e}")
            return False
        except Exception as e:
            self.logger.error(f"Unexpected error during connection check: {e}")
            return False

    def initialize_schema(self) -> bool:
        """
        Safely creates database tables if they do not exist.
        
        This is idempotent (safe to run multiple times). 
        In production, this is often skipped in favor of CI/CD migrations,
        but it serves as a reliable safety net for local development.
        """
        try:
            if not self.check_connection():
                return False

            self.logger.info("Initializing database schema (safe mode)...")
            self.db.create_all()
            
            inspector = inspect(self.db.engine)
            tables = inspector.get_table_names()
            self.logger.info(f"Schema verified. Existing tables: {tables}")
            
            return True
            
        except Exception as e:
            self.logger.exception(f"Database schema initialization failed: {e}")
            return False

    def dispose(self):
        """Disposes of the connection pool. Useful for testing or graceful shutdown."""
        try:
            self.db.engine.dispose()
            self.logger.info("Database engine disposed.")
        except Exception as e:
            self.logger.error(f"Error disposing engine: {e}")

# =============================================================================
# SECTION 3: Test Harness & Example Usage
# =============================================================================
if __name__ == "__main__":
    """
    Prerequisites: pip install flask flask-sqlalchemy
    Run: python database_manager.py
    """
    print("=" * 60)
    print("STARTING DATABASE MANAGER TEST HARNESS")
    print("=" * 60)
    
    app = Flask(__name__)
    db_uri = "sqlite:///test_industrial_db.sqlite"
    app.config['SQLALCHEMY_DATABASE_URI'] = db_uri
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    
    db = SQLAlchemy(app)
    
    class User(db.Model):
        __tablename__ = 'users'
        id = db.Column(db.Integer, primary_key=True)
        username = db.Column(db.String(80), unique=True, nullable=False)
        email = db.Column(db.String(120), unique=True, nullable=False)

    class Order(db.Model):
        __tablename__ = 'orders'
        id = db.Column(db.Integer, primary_key=True)
        user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
        total = db.Column(db.Float, nullable=False)
        user = db.relationship('User', backref=db.backref('orders', lazy=True))

    db_file = db_uri.replace("sqlite:///", "")
    if os.path.exists(db_file):
        os.remove(db_file)
        print(f"CLEANUP: Removed old test database: {db_file}")

    db_manager = DatabaseManager(db=db, app_logger=logger)
    
    try:
        with app.app_context():
            print("\n[TEST 1] Check Database Connection")
            if not db_manager.check_connection():
                print("[TEST 1] FAILED - Could not connect to database")
                sys.exit(1)
            print("[TEST 1] PASSED")
            
            print("\n[TEST 2] Initialize Schema (Idempotent)")
            if not db_manager.initialize_schema():
                print("[TEST 2] FAILED - Schema initialization failed")
                sys.exit(1)
            print("[TEST 2] PASSED")
            
            print("\n[TEST 3] Data Integrity Check (Write & Read)")
            try:
                new_user = User(username="FGci_cd_user", email="Ndevops@industrial.com")
                db.session.add(new_user)
                db.session.commit()
                print("  -> Successfully committed new user to database.")
                
                fetched_user = db.session.execute(
                    db.select(User).filter_by(username="ci_cd_user")
                ).scalar_one_or_none()
                
                if fetched_user and fetched_user.email == "devops@industrial.com":
                    print(f"  -> Successfully retrieved user: {fetched_user.username}")
                else:
                    print("  -> FAILED: Retrieved user does not match expected data.")
                    sys.exit(1)
            except Exception as e:
                print(f"  -> CRASHED with exception: {e}")
                import traceback
                traceback.print_exc()
                sys.exit(1)
            print("[TEST 3] PASSED")

            print("\n[TEST 4] Idempotency Check (Run initialization again)")
            if not db_manager.initialize_schema():
                print("[TEST 4] FAILED")
                sys.exit(1)
            print("[TEST 4] PASSED")

            print("\n[TEST 5] Cleanup Engine")
            db_manager.dispose()
            print("[TEST 5] PASSED")

    except Exception as e:
        print(f"\n[CRITICAL] Test harness failed with exception: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
        
    finally:
        print("\n" + "=" * 60)
        print("ENTERING FINALLY BLOCK (FILE SYSTEM CLEANUP)")
        print("=" * 60)
        if os.path.exists(db_file):
            os.remove(db_file)
            print(f"CLEANUP: Removed test database file: {db_file}")

    print("\n" + "=" * 60)
    print("✅ ALL DATABASE TESTS PASSED SUCCESSFULLY")
    print("=" * 60)