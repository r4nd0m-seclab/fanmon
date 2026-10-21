#!/usr/bin/env python3
"""
Chipset Thermal Ramp Daemon
Monitors X570 chipset fan speed/temp from ASUS EC.
- pwm4 (CHA_FAN3: X570 Aux Fans - two serial-connected fans): Managed dynamically tracking X570 chipset temperature.
    The aux fan is the fan physically on the PCH heatsink and is the dominant
    PCH cooler (case fans alone barely move it). Idle band is 60-65C.
    Floor: 60% (PWM 153) held through idle, i.e. <=65°C
    65°C - 72°C: Ramp 60% -> 100% (PWM 153 -> 255) so it pins by 72°C
    >72°C: 100% (PWM 255) pinned, holds PCH in the ~67-72°C band
- pwm1 (CHA_FAN1: Rear Exhaust) and pwm2 (CPU_FAN: Top Exhaust) are CLAMPED to
    the aux fan target (pwm4), so the exhaust fans pull PCH heat out in lockstep
    with the PCH cooler. A CPU safety floor (get_cpu_safety_pwm) is enforced as a
    lower bound so the CPU is still protected if it runs hot while the PCH is cool.
- pwm3 (CHA_FAN2: Front Intake Fans - 3x cluster): Hardware SmartFan IV curve
    tracking CPU PECI (capped at 80% / 204 PWM). FINAL ESCALATION: when the X570
    chipset passes 85C (everything else is already maxed), the front intake is
    driven to 100% (PWM 255) manually to shove maximum cool air through the case;
    when the chipset cools back to <=85C it returns to its hardware CPU curve.
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
    log("Daemon starting (pwm4=X570 Aux, pwm1/pwm2=Rear+Top Exhaust clamped to aux, pwm3=Front Intake [hw CPU curve])...")
    is_exhaust_clamped = False
    front_set = False
    front_escalated = False
    last_pwm4 = -1
    last_exhaust = -1
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
        # 60% floor is 153 PWM (~850 RPM), held through the 60-65C idle band.
        # The aux fan is the fan physically on the PCH heatsink, so it is the
        # dominant PCH cooler (case fans alone do little). Reworked to slam to
        # 100% by 72C so the chipset never creeps into the 80-84C WHEA-error zone.
        if chip_temp <= 65.0:
            target_pwm4 = 153
        elif chip_temp <= 72.0:
            frac = (chip_temp - 65.0) / 7.0
            target_pwm4 = int(153 + frac * 102)  # 153 -> 255 (100%)
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

        # 2. Front Intake (pwm3): normally on its hardware CPU-tracking curve.
        #    FINAL ESCALATION: when the chipset passes 85C (aux + exhaust already
        #    maxed), drive the front intake to 100% to shove max cool air through.
        if chip_temp > 85.0:
            if not front_escalated:
                log(f"Chipset {chip_temp:.1f}C > 85C. Driving Front Intake (pwm3) to 100% (final escalation).")
                safe_write(os.path.join(nct_dir, "pwm3_enable"), 1)
                safe_write(os.path.join(nct_dir, "pwm3"), 255)
                front_escalated = True
                front_set = True
        else:
            if front_escalated:
                log(f"Chipset cooled to {chip_temp:.1f}C (<=85C). Restoring Front Intake (pwm3) to hardware CPU curve.")
                safe_write(os.path.join(nct_dir, "pwm3_enable"), 5)
                front_escalated = False
            elif not front_set:
                # Normal (non-escalated) startup: put front on its hardware curve.
                safe_write(os.path.join(nct_dir, "pwm3_enable"), 5)
                front_set = True

        # 3. Clamp Rear Exhaust (pwm1) + Top Exhaust (pwm2) to the aux fan target
        #    (pwm4), so the exhaust pulls PCH heat out in lockstep with the PCH
        #    cooler. Enforce the CPU safety floor as a lower bound so the CPU is
        #    still protected if it runs hot while the PCH is cool.
        cpu_t = get_cpu_temp(nct_dir, k10_dir)
        cpu_floor = get_cpu_safety_pwm(cpu_t)
        exhaust_pwm = max(target_pwm4, cpu_floor)

        safe_write(os.path.join(nct_dir, "pwm1_enable"), 1)
        safe_write(os.path.join(nct_dir, "pwm2_enable"), 1)
        if abs(exhaust_pwm - last_exhaust) >= 3 or (now - last_log_time >= 30):
            safe_write(os.path.join(nct_dir, "pwm1"), exhaust_pwm)
            safe_write(os.path.join(nct_dir, "pwm2"), exhaust_pwm)
            pct = int(round(exhaust_pwm / 255.0 * 100))
            if abs(exhaust_pwm - last_exhaust) >= 3:
                floor_note = " (CPU floor)" if cpu_floor > target_pwm4 else ""
                log(f"Rear+Top Exhaust (pwm1/pwm2): {pct}% (PWM {exhaust_pwm}) clamped to aux{floor_note} at Chipset {chip_temp:.1f}C")
            last_exhaust = exhaust_pwm
            last_log_time = now
        is_exhaust_clamped = True

        time.sleep(2)

    log("Daemon stopping, restoring normal curves.")
    nct_dir, _, _ = find_hwmons()
    if nct_dir:
        safe_write(os.path.join(nct_dir, "pwm1_enable"), 5)
        safe_write(os.path.join(nct_dir, "pwm2_enable"), 5)
        safe_write(os.path.join(nct_dir, "pwm3_enable"), 5)
        safe_write(os.path.join(nct_dir, "pwm4_enable"), 1)
        safe_write(os.path.join(nct_dir, "pwm4"), 153)

if __name__ == "__main__":
    main()
