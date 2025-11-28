#!/bin/bash

# Simple production runner for this personal project using gunicorn
# Usage (on server):
#   chmod +x run_production.sh
#   ./run_production.sh

set -e

# Always run from the directory where this script lives
cd "$(dirname "$0")"

# Activate the virtual environment so we use the right Python + packages
if [ -d "venv" ]; then
  # shellcheck disable=SC1091
  . "venv/bin/activate"
fi

export FLASK_APP=app.py

# Bind on all interfaces, port 8000 (change if needed)
# IMPORTANT: use a single worker so in-memory DOWNLOAD_JOBS dictionary and
# download threads are shared by all requests. Multiple workers would each
# have their own copy and cause "Invalid job id" issues.
exec gunicorn -w 1 -b 0.0.0.0:8000 'app:app'




