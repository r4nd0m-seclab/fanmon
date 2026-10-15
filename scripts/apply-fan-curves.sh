#!/usr/bin/env bash
set -euo pipefail

NCT_DIR=""
for i in {1..15}; do
    for d in /sys/class/hwmon/hwmon*; do
        if [ -f "$d/name" ] && [ "$(cat "$d/name" 2>/dev/null)" = "nct6798" ]; then
            NCT_DIR="$d"
            break 2
        fi
    done
    sleep 1
done

if [ -n "$NCT_DIR" ]; then
    echo "Configuring Nuvoton NCT6798 at $NCT_DIR..."

    # PWM1 (CHA_FAN1: Rear Exhaust Fan)
    # Hardware SmartFan IV curve tracking CPU PECI (temp8) - capped at 80% (PWM 204)
    echo 8     > "$NCT_DIR/pwm1_temp_sel"
    echo 70    > "$NCT_DIR/pwm1_auto_point1_pwm"    # ~27% floor (~900 RPM)
    echo 45000 > "$NCT_DIR/pwm1_auto_point1_temp"   # 45C
    echo 115   > "$NCT_DIR/pwm1_auto_point2_pwm"    # ~45%
    echo 55000 > "$NCT_DIR/pwm1_auto_point2_temp"   # 55C
    echo 160   > "$NCT_DIR/pwm1_auto_point3_pwm"    # ~63%
    echo 65000 > "$NCT_DIR/pwm1_auto_point3_temp"   # 65C
    echo 204   > "$NCT_DIR/pwm1_auto_point4_pwm"    # ~80% max operational speed
    echo 75000 > "$NCT_DIR/pwm1_auto_point4_temp"   # 75C
    echo 255   > "$NCT_DIR/pwm1_auto_point5_pwm"    # Critical hardware safety fail-safe ceiling
    echo 85000 > "$NCT_DIR/pwm1_auto_point5_temp"   # 85C
    echo 5     > "$NCT_DIR/pwm1_enable"

    # PWM2 (CPU_FAN: UNPOPULATED - header removed from aux cooling)
    # Kept off. fan2 tach may read phantom RPM on the floating sense line.
    echo 1     > "$NCT_DIR/pwm2_enable" 2>/dev/null || true
    echo 0     > "$NCT_DIR/pwm2" 2>/dev/null || true

    # PWM3 (CHA_FAN2: Front Intake Fans - 3x daisy-chained cluster)
    # Hardware SmartFan IV curve tracking CPU PECI (temp8) - matching rear at 80% max (PWM 204)
    echo 8     > "$NCT_DIR/pwm3_temp_sel"
    echo 70    > "$NCT_DIR/pwm3_auto_point1_pwm"    # ~27% floor (~600-900 RPM)
    echo 45000 > "$NCT_DIR/pwm3_auto_point1_temp"   # 45C
    echo 115   > "$NCT_DIR/pwm3_auto_point2_pwm"    # ~45%
    echo 55000 > "$NCT_DIR/pwm3_auto_point2_temp"   # 55C
    echo 160   > "$NCT_DIR/pwm3_auto_point3_pwm"    # ~63%
    echo 65000 > "$NCT_DIR/pwm3_auto_point3_temp"   # 65C
    echo 204   > "$NCT_DIR/pwm3_auto_point4_pwm"    # ~80% max operational speed
    echo 75000 > "$NCT_DIR/pwm3_auto_point4_temp"   # 75C
    echo 255   > "$NCT_DIR/pwm3_auto_point5_pwm"    # Critical hardware safety fail-safe ceiling
    echo 85000 > "$NCT_DIR/pwm3_auto_point5_temp"   # 85C
    echo 5     > "$NCT_DIR/pwm3_enable"

    # PWM4 (CHA_FAN3: X570 Aux Fans - two serial-connected fans, chipset heatsink)
    # Starts at 70% floor (PWM 178); managed dynamically by chipset-fan-monitor.service
    echo 1     > "$NCT_DIR/pwm4_enable"
    echo 178   > "$NCT_DIR/pwm4"

    # PWM5 (Unpopulated)
    echo 1     > "$NCT_DIR/pwm5_enable" 2>/dev/null || true
    echo 0     > "$NCT_DIR/pwm5" 2>/dev/null || true
fi

for d in /sys/class/hwmon/hwmon*; do
    if [ -f "$d/name" ] && [ "$(cat "$d/name" 2>/dev/null)" = "kraken2023" ]; then
        echo "Found Kraken AIO at $d"
        echo 2 > "$d/pwm1_enable" 2>/dev/null || true
        echo 2 > "$d/pwm2_enable" 2>/dev/null || true
    fi
done
echo "Fan curves successfully applied."
