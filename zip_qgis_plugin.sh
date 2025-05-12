#!/bin/bash

# Script to zip the content of qgis_plugin directory at the last git commit and ensure credentials.json is included

# Set variables
PLUGIN_DIR="plugin"
OUTPUT_ZIP="$(pwd)/windscout_grunddaten.zip"
TEMP_DIR=$(mktemp -d)
NEW_DIR_NAME="windscout_grunddaten"

# Check if git is installed
if ! command -v git &> /dev/null; then
    echo "Error: git is not installed"
    exit 1
fi

# Check if we're in a git repository
if ! git rev-parse --is-inside-work-tree &> /dev/null; then
    echo "Error: Not in a git repository"
    exit 1
fi

# Get the last commit hash
LAST_COMMIT=$(git rev-parse HEAD)
echo "Zipping qgis_plugin directory as of commit: ${LAST_COMMIT}"

# Export the qgis_plugin directory at the last commit to the temp directory
git archive --format=tar "${LAST_COMMIT}" "${PLUGIN_DIR}" | tar -x -C "${TEMP_DIR}"

# Ensure credentials.json exists and is included in the plugin
if [ -f "${PLUGIN_DIR}/ogc_layer_handler/credentials.json" ]; then
    echo "Copying the current credentials.json file to the temporary directory"
    mkdir -p "${TEMP_DIR}/${PLUGIN_DIR}/ogc_layer_handler"
    cp "${PLUGIN_DIR}/ogc_layer_handler/credentials.json" "${TEMP_DIR}/${PLUGIN_DIR}/ogc_layer_handler/"
else
    echo "WARNING: credentials.json not found in the source directory"
    # Create a default credentials.json file
    mkdir -p "${TEMP_DIR}/${PLUGIN_DIR}/ogc_layer_handler"
    echo '{"organization":"myorg inc.", "api_key":"kwfbj234hdnoiq"}' > "${TEMP_DIR}/${PLUGIN_DIR}/ogc_layer_handler/credentials.json"
    echo "Created default credentials.json in the package"
fi

# Remove 'DEV' from name in metadata.txt
if [ -f "${TEMP_DIR}/${PLUGIN_DIR}/ogc_layer_handler/metadata.txt" ]; then
    echo "Removing 'DEV' string from metadata.txt name entry"
    sed -i 's/name=WindScout Grunddaten Dienst DEV/name=WindScout Grunddaten Dienst/g' "${TEMP_DIR}/${PLUGIN_DIR}/ogc_layer_handler/metadata.txt"
fi

# Create directory with new name
mkdir -p "${TEMP_DIR}/${NEW_DIR_NAME}"

# Copy the contents from ogc_layer_handler to the new directory
cp -r "${TEMP_DIR}/${PLUGIN_DIR}/ogc_layer_handler/"* "${TEMP_DIR}/${NEW_DIR_NAME}/"

# Create zip file directly from the new directory
# This creates a zip that can be directly installed in QGIS
(cd "${TEMP_DIR}" && zip -r "${OUTPUT_ZIP}" "${NEW_DIR_NAME}")

# Clean up the temp directory
rm -rf "${TEMP_DIR}"

echo "Created ${OUTPUT_ZIP} with directory renamed to ${NEW_DIR_NAME}, 'DEV' removed from metadata.txt, and including credentials.json" 
