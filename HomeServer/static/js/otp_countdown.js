document.addEventListener('DOMContentLoaded', function() {
    const badges = document.querySelectorAll('.otp-countdown');
    if (!badges.length) return; 

    function updateCountdowns() {
        const now = Math.floor(Date.now() / 1000);
        
        badges.forEach(badge => {
            const expStr = badge.dataset.expiresAt;
            if (!expStr) return;
            
            // Parse ISO date string to Unix timestamp
            const exp = Math.floor(new Date(expStr).getTime() / 1000);
            const remaining = exp - now;
            
            if (remaining <= 0) {
                // Session has expired
                badge.classList.remove('badge-success', 'badge-danger');
                badge.classList.add('badge-warning');
                badge.innerHTML = 'Expired';
                badge.title = 'This session has expired';
            } else {
                const mins = Math.floor(remaining / 60);
                const secs = remaining % 60;
                const label = mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
                badge.innerHTML = `Active &bull; ${label}`;
                badge.title = `Expires in ${label}`;
                
                // Visual warning: turn red if less than 60 seconds left
                if (remaining < 60) {
                    badge.classList.remove('badge-success');
                    badge.classList.add('badge-danger');
                } else {
                    badge.classList.remove('badge-danger');
                    badge.classList.add('badge-success');
                }
            }
        });
    }

    // Run immediately, then update every 1000ms (1 second)
    updateCountdowns();
    setInterval(updateCountdowns, 1000);
});