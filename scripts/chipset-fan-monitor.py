#!/usr/bin/env python3
"""
Chipset Thermal Ramp Daemon
Monitors X570 chipset fan speed/temp from ASUS EC.
- pwm1 (CHA_FAN1: Rear Exhaust): Hardware SmartFan IV curve tracking CPU PECI (capped at 80% / 204 PWM).
- pwm4 (CHA_FAN3: X570 Aux Fans - two serial-connected fans): Managed dynamically tracking X570 chipset temperature.
    Floor: 70% (PWM 178) at <=50°C
    50°C - 55°C: 70% (PWM 178)
    55°C - 65°C: Ramp 70% -> 85% (PWM 178 -> 217)
    65°C - 70°C: Ramp 85% -> 95% (PWM 217 -> 242)
    >70°C: 100% (PWM 255)
- pwm2 (CPU_FAN: UNPOPULATED) - header no longer in use, left off.
- pwm3 (CHA_FAN2: Front Intake Fans - 3x cluster): Hardware SmartFan IV curve tracking CPU PECI (matching rear, capped at 80% / 204 PWM).
- If chipset fan enters high-speed ramp (>80 PWM / >2800 RPM), pwm1 (Rear) and pwm3 (Front)
  are also temporarily boosted to flush case heat, matched and capped at 80% (PWM 204).
"""

import glob
import os
import signal
import sys
import time

def log(msg):
    print(f"[chipset-monitor] {msg}", flush=True)

running = True
def sig_handler(sig, frame):
    global running
    running = False

signal.signal(signal.SIGTERM, sig_handler)
signal.signal(signal.SIGINT, sig_handler)

def find_hwmons():
    nct_dir = None
    asusec_dir = None
    k10_dir = None
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            with open(os.path.join(d, "name")) as f:
                name = f.read().strip()
            if name.startswith("nct6798") or name.startswith("nct6775"):
                nct_dir = d
            elif name == "asusec":
                asusec_dir = d
            elif name == "k10temp":
                k10_dir = d
        except Exception:
            pass
    return nct_dir, asusec_dir, k10_dir

def safe_write(path, val):
    try:
        with open(path, "w") as f:
            f.write(str(val))
    except Exception as e:
        log(f"Error writing {val} to {path}: {e}")

def safe_read_int(path, default=0):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except Exception:
        return default

def get_cpu_temp(nct_dir, k10_dir):
    cpu_t = 0.0
    if k10_dir:
        t = safe_read_int(os.path.join(k10_dir, "temp1_input"), 0) / 1000.0
        if t > 0:
            cpu_t = max(cpu_t, t)
    if nct_dir:
        t = safe_read_int(os.path.join(nct_dir, "temp8_input"), 0) / 1000.0
        if t > 0:
            cpu_t = max(cpu_t, t)
    return cpu_t

def get_cpu_safety_pwm(cpu_t):
    """
    Matching 80% max curve for Front and Rear case fans:
    45°C: 70 PWM (~27% floor)
    55°C: 115 PWM (~45%)
    65°C: 160 PWM (~63%)
    75°C: 204 PWM (~80% max operational speed)
    """
    if cpu_t <= 45.0:
        return 70
    elif cpu_t <= 55.0:
        frac = (cpu_t - 45.0) / 10.0
        return int(70 + frac * 45)       # 70 -> 115
    elif cpu_t <= 65.0:
        frac = (cpu_t - 55.0) / 10.0
        return int(115 + frac * 45)      # 115 -> 160
    elif cpu_t <= 75.0:
        frac = (cpu_t - 65.0) / 10.0
        return int(160 + frac * 44)      # 160 -> 204
    else:
        return 204

def main():
    log("Daemon starting (pwm1=Rear [max 80%], pwm4=X570 Aux, pwm3=Front Intake [max 80%])...")
    is_case_ramping = False
    last_pwm4 = -1
    last_log_time = 0

    while running:
        nct_dir, asusec_dir, k10_dir = find_hwmons()
        if not asusec_dir or not nct_dir:
            time.sleep(2)
            continue

        chip_rpm = safe_read_int(os.path.join(asusec_dir, "fan1_input"), 0)
        chip_temp = safe_read_int(os.path.join(asusec_dir, "temp1_input"), 0) / 1000.0
        chip_pwm = (chip_rpm / 3500.0) * 100.0 if chip_rpm > 0 else 0.0
        now = time.time()

        # 1. Calculate X570 Aux Fan target PWM (pwm4 on CHA_FAN3 header, two serial fans)
        # 70% floor is 178 PWM (~1000 RPM)
        if chip_temp <= 50.0:
            target_pwm4 = 178
        elif chip_temp <= 55.0:
            target_pwm4 = 178
        elif chip_temp <= 65.0:
            frac = (chip_temp - 55.0) / 10.0
            target_pwm4 = int(178 + frac * 39)  # 178 -> 217 (85%)
        elif chip_temp <= 70.0:
            frac = (chip_temp - 65.0) / 5.0
            target_pwm4 = int(217 + frac * 25)  # 217 -> 242 (95%)
        else:
            target_pwm4 = 255

        # Ensure pwm4 is in manual mode
        current_en4 = safe_read_int(os.path.join(nct_dir, "pwm4_enable"), 0)
        if current_en4 != 1:
            safe_write(os.path.join(nct_dir, "pwm4_enable"), 1)

        if abs(target_pwm4 - last_pwm4) >= 3 or (now - last_log_time >= 30):
            safe_write(os.path.join(nct_dir, "pwm4"), target_pwm4)
            pct4 = int(round(target_pwm4 / 255.0 * 100))
            if abs(target_pwm4 - last_pwm4) >= 3:
                log(f"X570 Aux (pwm4): {pct4}% (PWM {target_pwm4}) at Chipset {chip_temp:.1f}C")
            last_pwm4 = target_pwm4
            last_log_time = now

        # 2. Case Fan Thermal Flush when Chipset exceeds 80 PWM
        # Both Rear Exhaust (pwm1) and Front Intake (pwm3) match, ramping up to 80% max (PWM 204)
        if chip_pwm > 80.0:
            ratio = min(1.0, max(0.0, (chip_pwm - 80.0) / 20.0))
            cpu_t = get_cpu_temp(nct_dir, k10_dir)

            boost_pwm = int(round(70 + ratio * (204 - 70)))
            cpu_floor = get_cpu_safety_pwm(cpu_t)
            boost_pwm = min(204, max(boost_pwm, cpu_floor))

            safe_write(os.path.join(nct_dir, "pwm1_enable"), 1)
            safe_write(os.path.join(nct_dir, "pwm1"), boost_pwm)
            safe_write(os.path.join(nct_dir, "pwm3_enable"), 1)
            safe_write(os.path.join(nct_dir, "pwm3"), boost_pwm)
            is_case_ramping = True
        else:
            if is_case_ramping:
                log(f"Chipset cooled ({chip_rpm} RPM). Restoring Hardware SmartFan IV curves on pwm1/pwm3.")
                safe_write(os.path.join(nct_dir, "pwm1_enable"), 5)
                safe_write(os.path.join(nct_dir, "pwm3_enable"), 5)
                is_case_ramping = False

        time.sleep(2)

    log("Daemon stopping, restoring normal curves.")
    nct_dir, _, _ = find_hwmons()
    if nct_dir:
        safe_write(os.path.join(nct_dir, "pwm1_enable"), 5)
        safe_write(os.path.join(nct_dir, "pwm2_enable"), 1)
        safe_write(os.path.join(nct_dir, "pwm2"), 0)
        safe_write(os.path.join(nct_dir, "pwm3_enable"), 5)
        safe_write(os.path.join(nct_dir, "pwm4_enable"), 1)
        safe_write(os.path.join(nct_dir, "pwm4"), 178)

if __name__ == "__main__":
    main()
