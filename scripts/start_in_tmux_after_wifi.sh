#!/bin/bash

echo "Running script: $1"
echo "In tmux window: $2"

# Wait until WiFi is up by checking for an IP address
while ! ping -c1 -W1 8.8.8.8 >/dev/null 2>&1; do
	echo "No wifi found, sleeping"
	sleep 5
done

echo "WiFi found, starting script"
tmux new-session -d -s $2 "~/.venv/bin/python ~/sugar_house_monitor/scripts/$1 > ~/script.log"
