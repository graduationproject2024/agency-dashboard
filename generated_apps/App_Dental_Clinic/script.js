document.addEventListener('DOMContentLoaded', () => {
    // Smooth scrolling for navigation
    document.querySelectorAll('a[href^="#"]').forEach(anchor => {
        anchor.addEventListener('click', function (e) {
            e.preventDefault();
            document.querySelector(this.getAttribute('href')).scrollIntoView({
                behavior: 'smooth'
            });
        });
    });

    // Form submission handling
    const bookingForm = document.getElementById('bookingForm');
    const formMessage = document.getElementById('formMessage');

    bookingForm.addEventListener('submit', (e) => {
        e.preventDefault();
        
        const name = document.getElementById('name').value;
        const service = document.getElementById('service').value;

        // Show success message
        formMessage.textContent = `تم استلام طلبك بنجاح يا ${name}! سنتواصل معك قريباً لتأكيد موعد ${service}.`;
        formMessage.classList.remove('hidden');
        
        // Reset form
        bookingForm.reset();

        // Hide message after 5 seconds
        setTimeout(() => {
            formMessage.classList.add('hidden');
        }, 5000);
    });
});
