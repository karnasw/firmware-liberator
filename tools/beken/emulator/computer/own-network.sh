# shellcheck shell=bash disable=SC2034  # read by those that source it
# The device's own network, carried as Ethernet between the device's container
# and the computers on its Docker network in a VXLAN tunnel: device-net.sh
# bridges the device's station (device.py --own-tap) onto it, and a computer
# joins it with `wifi join` (bin/wifi). Its frames reach every container on
# that Docker network that has joined the group, and nothing beyond it.
OWN_VNI=42
OWN_GROUP=239.255.42.1
OWN_PORT=4789
