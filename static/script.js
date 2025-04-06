function sendCommand(command) {
    console.log(`Sending command: ${command}`);
    fetch("/update", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ command }),
    })
    .then(response => {
        console.log(`Response status: ${response.status}`);
        return response.json();
    })
    .then(data => {
        console.log("Response data:", data);
        updatePage(data);
    })
    .catch(error => console.error("Error sending command:", error));
}

function updatePage(data) {
    console.log("Data received in updatePage:", data);


    // Update text fields
    document.getElementById("total_current_gallons").innerText = data.combined_gals;
    document.getElementById("total_rate").innerText = data.combined_rate;
    document.getElementById("total_timing").innerText = data.timing_est_str;

    document.getElementById("brookside_current_gallons").innerText = data.brookside.current_gallons;
    document.getElementById("brookside_rate_str").innerText = data.brookside.rate_str;
    document.getElementById("brookside_remaining_time").innerText = data.brookside.remaining_time;
    document.getElementById("brookside_sap_surface").innerText = data.brookside.dist_to_surf;
    document.getElementById("brookside_sap_depth").innerText = data.brookside.depth;
    
    document.getElementById("roadside_current_gallons").innerText = data.roadside.current_gallons;
    document.getElementById("roadside_rate_str").innerText = data.roadside.rate_str;
    document.getElementById("roadside_remaining_time").innerText = data.roadside.remaining_time;
    document.getElementById("roadside_sap_surface").innerText = data.roadside.dist_to_surf;
    document.getElementById("roadside_sap_depth").innerText = data.roadside.depth;

    
    document.getElementById("rate_window").innerText = data.roadside.mins_back;
   

    document.getElementById("system_time").innerText = data.system_time;
}


document.addEventListener("DOMContentLoaded", () => {
    const updateInterval = 1000*15; // Update every second

    
    function fetchUpdate() {
        sendCommand("update",document);
    }


    // Attach event listeners to buttons
    document.querySelectorAll("button[data-command]").forEach(button => {
        button.addEventListener("click", () => {
            const command = button.getAttribute("data-command");
            sendCommand(command);
        });
    });

    // Start the update loop
    setInterval(fetchUpdate, updateInterval);
});
