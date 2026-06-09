from gevent import monkey
monkey.patch_all()

import os
import sys

os.environ['GEVENT_SUPPORT'] = 'False'
os.environ['FLASK_DEBUG'] = '0'  

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


from HomeServer import create_app, socketio
def main():
    """Main entry point for the ElectroNora SmartHome System."""
    try:
        from HomeServer.services import configurations as cfg
        
        app, socketio_instance = create_app()
        
        print("\n" + "="*60)
        print("🚀 ElectroNora SmartHome System")
        print("="*60)
        print(f"📁 Project Root: {os.path.dirname(os.path.abspath(__file__))}")
        print(f"🔧 Debug Mode: {cfg.DEBUG}")
        print(f"🔄 Auto-Reload: {'Enabled' if cfg.AUTO_RELOAD else 'Disabled'}")
        print(f"🌍 Host: {cfg.HOST}")
        print(f"🚪 Port: {cfg.PORT}")
        print(f"📊 Database: {app.config.get('SQLALCHEMY_DATABASE_URI', 'Not set')}")
        print(f"🔒 Secret Key: {'Set' if app.secret_key else 'Not set'}")
        print("="*60 + "\n")

        socketio_instance.run(
            app,
            host=cfg.HOST,
            port=cfg.PORT,
            debug=cfg.DEBUG,
            use_reloader=cfg.AUTO_RELOAD, 
            log_output=True,
        )
        
    except KeyboardInterrupt:
        print("\n[INFO] Server shutdown requested by user. Exiting gracefully...")
        sys.exit(0)
    except Exception as e:
        print(f"\n[ERROR] An unexpected error occurred: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()