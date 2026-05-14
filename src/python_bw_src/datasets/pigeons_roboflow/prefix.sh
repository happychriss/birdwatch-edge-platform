#!/bin/bash

# Check if directory path is provided as an argument
if [ "$#" -ne 1 ]; then
    echo "Usage: $0 <directory>"
    exit 1
fi

# Directory path from the command line argument
directory="$1"

# Loop through all jpg files in the specified directory
for file in "$directory"/*.jpg; do
    # Extract filename from path
    filename=$(basename "$file")
    # Check if the file exists to avoid errors in case of empty matches
    if [ -f "$file" ]; then
        # Move and rename the file by adding the prefix
        mv "$file" "$directory/no_bird.$filename"
    fi
done
