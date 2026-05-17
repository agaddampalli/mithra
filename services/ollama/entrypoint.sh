#!/bin/bash
set -e

# Start ollama server in the background
ollama serve &
OLLAMA_PID=$!

# Wait until the server accepts connections
until curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; do
    sleep 1
done

# Pull phi3:mini only if not already in the volume (persists across restarts)
if ! ollama list 2>/dev/null | grep -q "phi3:mini"; then
    echo "[ollama-entrypoint] Pulling phi3:mini (first start only)..."
    ollama pull phi3:mini
    echo "[ollama-entrypoint] phi3:mini ready"
else
    echo "[ollama-entrypoint] phi3:mini already present, skipping pull"
fi

# Hand off to the server process
wait $OLLAMA_PID
