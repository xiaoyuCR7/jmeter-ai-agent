#!/bin/bash

echo "=========================================="
echo "  JMeter Agent - AI-Powered Performance Testing"
echo "=========================================="
echo ""

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Allow overriding the backend port via the PORT environment variable
export PORT="${PORT:-8000}"

echo "Checking backend port $PORT availability..."
port_in_use=0
if command -v lsof >/dev/null 2>&1; then
    if lsof -i ":$PORT" >/dev/null 2>&1; then
        port_in_use=1
    fi
elif command -v ss >/dev/null 2>&1; then
    if ss -tlnp 2>/dev/null | grep -q ":$PORT "; then
        port_in_use=1
    fi
fi

if [ "$port_in_use" -eq 1 ]; then
    echo "ERROR: Port $PORT is already in use."
    echo ""
    echo "Possible solutions:"
    echo "  1. Stop the existing service on port $PORT."
    echo "  2. Use a different port by running:"
    echo "       PORT=8001 ./start.sh"
    echo ""
    exit 1
fi

echo "[1/2] Starting backend service on port $PORT..."
cd "$SCRIPT_DIR/jmeter-agent-backend" || exit 1
ACCESS_URL="$(python3 -m app.core.local_access | tail -n 1)"
python3 -m app.main &
BACKEND_PID=$!

echo "[2/2] Starting frontend dev server..."
cd "$SCRIPT_DIR/jmeter-agent-frontend" || exit 1
npm run dev &
FRONTEND_PID=$!

echo ""
echo "=========================================="
echo "Services started successfully!"
echo "Open:     $ACCESS_URL"
echo "Backend:  http://localhost:$PORT"
echo "API Docs: http://localhost:$PORT/api/docs"
echo "=========================================="
echo ""
echo "Note: Press Ctrl+C to stop all services."
echo ""

if command -v xdg-open >/dev/null 2>&1; then OPENER=xdg-open
elif command -v open >/dev/null 2>&1; then OPENER=open
fi
if [ -n "$OPENER" ]; then (sleep 5; "$OPENER" "$ACCESS_URL" >/dev/null 2>&1) & fi

# Wait for both processes
wait $BACKEND_PID
wait $FRONTEND_PID
