#!/bin/bash

# Run the Flask app on localhost:3000
export FLASK_APP=app.py
export FLASK_RUN_HOST=localhost
export FLASK_RUN_PORT=3000

./venv/bin/flask run

