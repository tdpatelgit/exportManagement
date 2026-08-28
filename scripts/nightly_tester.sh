#!/bin/bash

# Nightly test runner script for Ledger CRM
# This script runs the test suite every night at 11 PM

# Set the project directory
PROJECT_DIR="/Users/tejasdharmendrapatel/Documents/Tejas/ExportManagement/exportManagement"
LOG_FILE="$PROJECT_DIR/logs/nightly-test-run-$(date +%F).log"

# Change to project directory
cd "$PROJECT_DIR" || exit 1

# Activate virtual environment and run tests
echo "Starting nightly test run at $(date)" >> "$LOG_FILE"
echo "Project: $PROJECT_DIR" >> "$LOG_FILE"

if [ -f "./CRMenv/bin/activate" ]; then
    source ./CRMenv/bin/activate
    echo "Virtual environment activated" >> "$LOG_FILE"
else
    echo "Virtual environment not found!" >> "$LOG_FILE"
    exit 1
fi

# Run tests with pytest
echo "Running pytest..." >> "$LOG_FILE"
cd crm_app
if ../CRMenv/bin/python -m pytest tests/ -v --tb=short >> "$LOG_FILE" 2>&1; then
    echo "Tests completed successfully at $(date)" >> "$LOG_FILE"
    echo "SUCCESS: All tests passed!" >> "$LOG_FILE"
else
    echo "Tests failed at $(date)" >> "$LOG_FILE"
    echo "FAILURE: Some tests failed!" >> "$LOG_FILE"
fi

echo "Nightly test run completed at $(date)" >> "$LOG_FILE"
echo "----------------------------------------" >> "$LOG_FILE"
