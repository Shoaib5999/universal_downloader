#!/bin/bash

# Simple production runner for this personal project using gunicorn
# Usage (on server):
#   chmod +x run_production.sh
#   ./run_production.sh

set -e

export FLASK_APP=app.py

# Bind on all interfaces, port 8000 (change if needed)
exec gunicorn -w 2 -b 0.0.0.0:8000 'app:app'




