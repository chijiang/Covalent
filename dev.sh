#!/bin/bash
# Development server startup script for Agent Framework
# Loads configuration from .env file and starts both backend and frontend

set -a
[ -f .env ] && source .env
set +a

# Backend port (default: 5170)
BACKEND_PORT=${AGENT_FRAMEWORK_BACKEND_PORT:-5170}
BACKEND_HOST=${AGENT_FRAMEWORK_BACKEND_HOST:-0.0.0.0}

# Frontend port (default: 3100)
FRONTEND_PORT=${AGENT_FRAMEWORK_FRONTEND_PORT:-3100}

echo "================================"
echo "Agent Framework Development Server"
echo "================================"
echo "Backend:  http://$BACKEND_HOST:$BACKEND_PORT"
echo "Frontend: http://localhost:$FRONTEND_PORT"
echo "================================"
echo ""

# Check if command provided
if [ "$1" == "backend" ]; then
    echo "Starting backend on port $BACKEND_PORT..."
    python main.py serve --port $BACKEND_PORT
elif [ "$1" == "frontend" ]; then
    echo "Starting frontend on port $FRONTEND_PORT..."
    cd frontend
    PORT=$FRONTEND_PORT npm run dev
elif [ "$1" == "both" ] || [ -z "$1" ]; then
    echo "Starting both backend and frontend..."
    echo ""
    
    # Start backend in background
    echo "[Backend] Starting on port $BACKEND_PORT..."
    python main.py serve --port $BACKEND_PORT &
    BACKEND_PID=$!
    
    sleep 2
    
    # Start frontend
    echo "[Frontend] Starting on port $FRONTEND_PORT..."
    cd frontend
    PORT=$FRONTEND_PORT npm run dev
    
    # If frontend exits, kill backend
    kill $BACKEND_PID 2>/dev/null
else
    echo "Usage: ./dev.sh [backend|frontend|both]"
    echo ""
    echo "Examples:"
    echo "  ./dev.sh backend     - Start only backend"
    echo "  ./dev.sh frontend    - Start only frontend"
    echo "  ./dev.sh both        - Start both (default)"
    echo "  ./dev.sh             - Start both (default)"
    exit 1
fi
