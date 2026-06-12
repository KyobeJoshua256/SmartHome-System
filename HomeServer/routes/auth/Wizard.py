from flask import Blueprint, render_template, request, redirect, url_for, flash
from werkzeug.routing import BuildError
from HomeServer import database
from HomeServer.models import User, UserRole


wizard = Blueprint('wizard', __name__)

def safe_redirect(endpoint: str, fallback_url: str = '/login'):
    """Safely redirects, falling back to a direct URL if the endpoint doesn't exist."""
    try:
        return redirect(url_for(endpoint))
    except BuildError:
        return redirect(fallback_url)

@wizard.route('/', methods=['GET', 'POST'])
def setup_wizard():
    """First-run setup wizard to create the initial Admin user."""
    if User.query.count() > 0:
        flash('System is already initialized. Please log in.', 'warning')
        return safe_redirect('auth.login', '/login')
        
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        phone = request.form.get('phone', '').strip()
        password = request.form.get('password', '')

        if not all([username, email, phone, password]):
            flash('All fields are required.', 'danger')
            return render_template('auth/setup.html')

        try:
            # Create the first admin
            new_admin = User(
                username=username,
                email=email,
                phone=phone,
                role=UserRole.ADMIN.value,
                # is_active is a presence flag (True=online, False=offline).
                # A newly created account starts offline; it becomes True
                # automatically when the admin logs in for the first time.
                is_active=False,
                phone_verified=True,  
                otp_enabled=False
            )
            
            # This uses your optimized Argon2id hashing
            new_admin.set_password(password) 
            
            database.session.add(new_admin)
            database.session.commit()
            
            flash('Admin account created successfully! Redirecting to login...', 'success')
            return safe_redirect('auth.login', '/login')
            
        except Exception as e:
            database.session.rollback()
            error_msg = str(e).lower()
            
            # Provide meaningful, user-friendly error messages instead of raw SQL errors
            if 'unique constraint' in error_msg or 'unique' in error_msg:
                if 'username' in error_msg:
                    flash('That username is already taken. Please choose another.', 'danger')
                elif 'email' in error_msg:
                    flash('That email address is already registered.', 'danger')
                elif 'phone' in error_msg:
                    flash('That phone number is already in use.', 'danger')
                else:
                    flash('A user with those details already exists.', 'danger')
            elif 'invalid phone number' in error_msg:
                flash('Invalid phone number format. Please use international format (e.g., +256...).', 'danger')
            elif 'invalid email' in error_msg:
                flash('Please enter a valid email address.', 'danger')
            else:
                flash(f'Error creating admin: {str(e)}', 'danger')
            
            return render_template('auth/setup.html')

    return render_template('auth/setup.html')