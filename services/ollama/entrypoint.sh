#!/bin/sh
# Starts a temporary ollama server to pull phi3:mini into the volume,
# then replaces itself with the real server via exec.
# On subsequent starts the model is already in the volume so pull is skipped.

echo "[ollama-entrypoint] Starting setup server..."
ollama serve &
SETUP_PID=$!

# Wait for the server to accept connections using the ollama CLI itself
# (curl may not be present in this image)
ATTEMPTS=0
until ollama list > /dev/null 2>&1; do
    ATTEMPTS=$((ATTEMPTS + 1))
    if [ "$ATTEMPTS" -ge 30 ]; then
        echo "[ollama-entrypoint] ERROR: server did not start after 60s"
        kill "$SETUP_PID" 2>/dev/null
        exit 1
    fi
    sleep 2
done

echo "[ollama-entrypoint] Server ready"

# Pull phi3:mini only if absent (volume persists across restarts)
if ollama list 2>/dev/null | grep -q "phi3:mini"; then
    echo "[ollama-entrypoint] phi3:mini already present, skipping pull"
else
    echo "[ollama-entrypoint] Pulling phi3:mini (first start — takes a few minutes)..."
    ollama pull phi3:mini
    echo "[ollama-entrypoint] phi3:mini ready"
fi

# Stop the setup server cleanly
kill "$SETUP_PID" 2>/dev/null
wait "$SETUP_PID" 2>/dev/null || true

echo "[ollama-entrypoint] Handing off to production server"
# exec replaces this shell — ollama becomes PID 1 and handles signals correctly
exec ollama serve
