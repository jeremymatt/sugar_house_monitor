function toggleLight() {
    fetch('/set_light', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        }
    })
    .then(response => {
        if (response.ok) {
            location.reload(); // Reload the page to update the light status
        } else {
            alert('Failed to toggle light');
        }
    })
    .catch(error => {
        console.error('Error:', error);
    });
}

function toggleDoor() {
    const newStatus = document.getElementById('new-door-status').value || 'Override';

    fetch('/set_door', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({ new_status: newStatus })
    })
    .then(response => {
        if (response.ok) {
            location.reload(); // Reload the page to update the door status
        } else {
            alert('Failed to toggle door');
        }
    })
    .catch(error => {
        console.error('Error:', error);
    });
}
