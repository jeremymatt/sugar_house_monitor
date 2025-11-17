# Sugar house monitor
Planned functionality includes
1. Monitoring sap levels in each tank
1. Projecting time to full/empty
1. Monitoring the vacuum in the line system
1. Montitoring the stack temperature


# Install/setup notes:
1. Enable UART (hardware serial) with raspi-config:
    * sudo raspi-config nonint do_serial_cons 1
    * sudo raspi-config nonint do_serial_hw 0
1. Enable a second UART port:
    * sudo vi /boot/firmware/config.txt
    * Make sure the following is at the bottom of the file (un-comment the port  you want to use):
    ```
    enable_uart=1
    # dtoverlay=uart2   #TX: GPIO0 /   RX: GPIO1
    # dtoverlay=uart3   #TX: GPIO4 /   RX: GPIO5 
    # dtoverlay=uart4   #TX:CE01    /   RX:MISO
    dtoverlay=uart5   #TX: GPIO12 /   RX: GPIO13
    ```
    
# Setup on wordpress site
1. Clone the https://github.com/jeremymatt/sugar_house_monitor to `~/git/`
1. `ln -s ~/git/sugar_house_monitor/web \~/mattsmaplesyrup.com/sugar_house_monitor` to create a symlink from the sugar_house_monitor directory to the web directory in the git repo
