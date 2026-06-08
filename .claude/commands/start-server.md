#!/bin/bash
# Start the PE Dashboard server
description: Start the FastAPI PE Dashboard server on port 8765

py -3.14 -m uvicorn main:app --host 127.0.0.1 --port 8765
