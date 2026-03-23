#!/bin/bash
# Load .env file
export $(grep -v '^#' .env | xargs)
# Start the server
python3 server.py
