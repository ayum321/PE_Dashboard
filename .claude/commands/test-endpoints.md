#!/bin/bash
# Run a quick test against the running server
description: Test key API endpoints are responding

$uri = "http://127.0.0.1:8765"
Write-Host "Testing PE Dashboard endpoints..."
Invoke-RestMethod "$uri/api/config" -Method GET | ConvertTo-Json -Depth 3
Write-Host "Config OK"
Invoke-RestMethod "$uri/api/sla-debug" -Method GET | ConvertTo-Json -Depth 3
Write-Host "SLA Debug OK"
Write-Host "All endpoints responding."
