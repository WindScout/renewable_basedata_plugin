# WindScout Grunddaten Dienst QGIS Plugin

A QGIS plugin that provides streamlined access to WindScout base data sets services through an API key-authenticated proxy. This plugin enables access to renewable energy base data services.

## Features

- **Layer Tree Structure**: Organized access to federated OGC services
- **Authentication**: Secure API key-based access
- **Service Discovery**: Automatically loads available layers from configuration
- **TinyOWS Integration**: Direct access to internal TinyOWS services
- **Pre-configured Credentials**: Can be distributed with built-in authentication
- **Renewable Energy Data**: Specialized data layers for wind and solar energy planning
  
## Development

Install plugin "Plugin Reloader"

Symlink plugin source code to QGIS plugin folder (this is for linux)
```bash
# Snyc locally for testing (Linux/macOS), 

#when having installed QGIS through apt repo
ln -s "$(pwd)/windscout_grunddaten" ~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/windscout_grunddaten

# when having installed through flatpak
ln -s "$(pwd)/windscout_grunddaten" ~/.var/app/org.qgis.qgis/data/QGIS/QGIS3/profiles/default/python/plugins/windscout_grunddaten


```

## Installation

1. Get the plugin ZIP file (`windscout_grunddaten.zip`)
2. In QGIS: **Plugins → Manage and Install Plugins → Install from ZIP**
3. Select the downloaded ZIP file and click **Install Plugin**
4. The plugin will appear in the **Web** menu as **WindScout Grunddaten Dienst**

## Quick Start

### Server Configuration

1. Go to **Web → WindScout Grunddaten Dienst → Configure Server**
2. Enter the hostname provided by WindScout
3. Provide your organization name and API key provided by WindScout
4. Check "Save API key in QGIS Auth Database" for secure storage

### Loading Layers

1. Go to **Web → WindScout Grunddaten Dienst → Load Layers**
2. Browse the organized layer tree and add desired layers to your project

## For Administrators

### Distributing Pre-configured Plugin

Create a `credentials.json` file in the plugin directory:
```json
{
  "organization": "myorg inc.",
  "api_key": "kwfbj234hdnoiq"
}
```

### Building the Plugin

You can build the latest version of the plugin using the provided script:
```bash
./zip_qgis_plugin.sh
```
This will create a zip file with the content of the qgis_plugin directory at the last git commit. The script automatically includes the `credentials.json` file in the package, either by copying the existing file or creating a default one if it doesn't exist.

### Testing Connectivity

The plugin includes three testing options:
- **Test TinyOWS**: Verifies connection to TinyOWS service
- **Test Auth Configuration**: Validates authentication setup
- **Test Direct HTTP Request**: Tests raw API key authentication

## Troubleshooting

- **Authentication Issues**: Verify API key and server URL
- **Layer Loading Problems**: Check QGIS log panel for error messages
- **Empty Layer List**: Ensure server connection is working and credentials are valid
- **Log File**: Check the log.log file in the plugin directory for detailed error information

## Version Information

Current version: 1.27
QGIS minimum version: 3.0
Author: RL (info@windscout.de)
