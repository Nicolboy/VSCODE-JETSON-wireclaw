#!/bin/bash

echo "=== WireClaw Startup ==="

# 1. Nettoyage
echo "Nettoyage..."
pkill -f "hermes" 2>/dev/null
pkill -f "arducopter" 2>/dev/null
pkill -f "mavproxy" 2>/dev/null
pkill -f "wireclaw_core" 2>/dev/null
pkill -f "uvicorn" 2>/dev/null
pkill -f "web_server" 2>/dev/null
sleep 2
sudo fuser -k 14550/udp 14551/udp 14552/udp 2>/dev/null
sleep 1

# 2. Lancer ArduCopter SITL directement
echo "Lancement ArduCopter..."
/home/nicoboy/ardupilot/build/sitl/bin/arducopter \
    --model + \
    --speedup 1 \
    --sim-address=127.0.0.1 \
    --defaults /home/nicoboy/wireclaw/sitl_params.parm \
    > /tmp/arducopter.log 2>&1 &
sleep 3

# 3. Lancer MAVProxy directement
echo "Lancement MAVProxy..."
/home/nicoboy/.local/bin/mavproxy.py \
    --master tcp:127.0.0.1:5760 \
    --sitl 127.0.0.1:5501 \
    --out udp:127.0.0.1:14551 \
    --out udp:127.0.0.1:14552 \
    --daemon \
    > /tmp/arducopter.log 2>&1 &
sleep 2

# 4. Attendre GPS fix
echo "Attente GPS fix..."
timeout=120
while [ $timeout -gt 0 ]; do
    if grep -q "IMU0 is using GPS" /tmp/arducopter.log 2>/dev/null; then
        echo ""
        echo "✅ SITL prêt !"
        break
    fi
    sleep 2
    timeout=$((timeout-2))
    echo -n "."
done

if [ $timeout -le 0 ]; then
    echo ""
    echo "❌ Timeout — logs :"
    tail -10 /tmp/arducopter.log
    tail -10 /tmp/arducopter.log
    exit 1
fi

sleep 2

# 5. Lancer WireClaw
echo "Lancement WireClaw..."
cd ~/wireclaw
uvicorn web_server:app --host 0.0.0.0 --port 8765
