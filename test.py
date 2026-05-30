# test_heartbeat_sender.py
from pymavlink import mavutil
import time

conn = mavutil.mavlink_connection("udpout:127.0.0.1:14550")
while True:
    conn.mav.heartbeat_send(
        mavutil.mavlink.MAV_TYPE_QUADROTOR,
        mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA,
        0, 0,
        mavutil.mavlink.MAV_STATE_STANDBY,
    )
    print("heartbeat sent")
    time.sleep(1)