"""
QGIS Plugin providing WindScout basic datasets for renewables project development

This plugin loads data from Geoserver instances via NGINX Proxy, including both external OGC services 
internal TinyOWS services, and XYZ tile services. It reads configuration from a JSON file and
provides robust error handling and authentication support.

"""

from __future__ import annotations
import os
import logging
import traceback
import json
import base64
import re
from datetime import datetime
from typing import Dict, Optional, Tuple, Union

from qgis.PyQt.QtWidgets import (
    QAction, QHBoxLayout,QFileDialog, 
    QDialog, QVBoxLayout, QFormLayout, QLineEdit, 
    QPushButton, QLabel, QCheckBox,  QMessageBox, QTabWidget, QWidget
)

from qgis.PyQt.QtCore import QUrl, QByteArray
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply

from qgis.core import (
    QgsVectorLayer,
    QgsRasterLayer,
    QgsProject,
    QgsLayerTreeGroup,
    QgsSingleSymbolRenderer,
    QgsFillSymbol,
    QgsLineSymbol,
    QgsLayerMetadata,
    QgsBox3d,
    QgsDateTimeRange,
    QgsAuthMethodConfig,
    QgsApplication,
    Qgis,
    QgsCoordinateTransform,
    QgsMarkerSymbol,
    QgsSettings,
    QgsDataSourceUri,
    QgsMapLayer,
    QgsNetworkAccessManager,
    QgsNetworkReplyContent,
)

import hashlib
import glob
import cProfile
import pstats
import io
import concurrent.futures

from .tools import LayerHandler, setup_logging, CredentialManager

# --- PLUGIN_CODE_VERSION calculation updated to include all local code files ---
def _get_combined_code_hash():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    py_files = sorted(glob.glob(os.path.join(base_dir, '*.py')))
    md5 = hashlib.md5()
    for fname in py_files:
        with open(fname, 'rb') as f:
            md5.update(f.read())
    return md5.hexdigest()[:8]

PLUGIN_CODE_VERSION = _get_combined_code_hash()



class MetadataHandler:
    """
    Handles layer metadata with caching and optimized loading
    
    Retrieves metadata from JSON configuration files and remote services.
    """
    
    def __init__(self, config_path: str = None):
        """
        Initialize the metadata handler.
        
        Args:
            config_path: Path to the JSON configuration file
        """
        self.config = {}
        self.credential_manager = None
        if config_path:
            try:
                with open(config_path, 'r') as f:
                    self.config = json.load(f)
                    self.logger = logging.getLogger('qgis_plugin')
            except FileNotFoundError:
                # Handle case when config file does not exist
                self.logger = logging.getLogger('qgis_plugin')
                self.logger.warning(f"Config file not found: {config_path}, using empty configuration")
            except json.JSONDecodeError:
                self.logger = logging.getLogger('qgis_plugin')
                self.logger.warning(f"Invalid JSON in config file: {config_path}, using empty configuration")
            except Exception as e:
                self.logger = logging.getLogger('qgis_plugin')
                self.logger.error(f"Error loading config file: {e}")
        else:
            self.logger = logging.getLogger('qgis_plugin')

    def set_credential_manager(self, credential_manager):
        """
        Set the credential manager to use for authentication.
        
        Args:
            credential_manager: CredentialManager instance
        """
        self.credential_manager = credential_manager

    def fetch_tinyows_metadata(self, collection_id: str) -> Optional[Dict]:
        """
        Fetch metadata for TinyOWS layers.
        
        Args:
            collection_id: Collection ID
            
        Returns:
            Dict: Metadata or None if request failed
        """
        try:
            # Get hostname and port from QgsSettings first, falling back to config
            settings = QgsSettings()
            hostname = settings.value("ogc_layer_handler/config_hostname")
            port = settings.value("ogc_layer_handler/config_port")
            
            # If not found in settings, use values from config
            if not hostname:
                hostname = self.config.get('hostname', 'localhost')
            
            if not port:
                port = self.config.get('port', 443)
                
            if hostname in ['127.0.0.1', 'localhost']:
                protocol = 'http'
            else:
                protocol = 'https'
                port = '443'
            # Capabilities URL for specific layer
            capabilities_url = (
                f"{protocol}://{hostname}:{port}/tinyows?"
                f"service=WFS&"
                f"version=1.1.0&"
                f"request=DescribeFeatureType&"
                f"typename={collection_id}"
            )
            
            # Create network request
            request = QNetworkRequest(QUrl(capabilities_url))
            
            # Get authentication header if credential manager is set
            if self.credential_manager:
                auth_header = self.credential_manager.get_auth_header()
                if auth_header:
                    for header_name, header_value in auth_header.items():
                        request.setRawHeader(
                            QByteArray(header_name.encode()),
                            QByteArray(str(header_value).encode())
                        )
                
                # Also apply authentication configuration if available
                auth_config_id = self.credential_manager.get_auth_config_id()
                if auth_config_id:
                    auth_manager = QgsApplication.authManager()
                    auth_manager.updateNetworkRequest(request, auth_config_id)
            
            # Make blocking request
            nam = QgsNetworkAccessManager.instance()
            reply = nam.blockingGet(request)
            
            # Check if request was successful
            if reply.error() == QNetworkReply.NoError:
                # TinyOWS returns XML schema, but we can extract basic info
                return {
                    'id': collection_id,
                    'title': f"TinyOWS Layer: {collection_id}",
                    'description': f"WFS layer from TinyOWS service"
                }
            else:
                self.logger.warning(f"Failed to fetch metadata for collection {collection_id}: {reply.errorString()}")
                
        except Exception as e:
            self.logger.error(f"Error fetching TinyOWS metadata: {str(e)}")
            
        return None

    def fetch_external_metadata(self, service_config: Dict, collection_id: str) -> Optional[Dict]:
        """
        Fetch metadata from external WFS service through the proxy.
        
        Args:
            service_config: Service configuration
            collection_id: Collection ID
            
        Returns:
            Dict: Metadata or None if request failed
        """
        try:
            # Get hostname and port from QgsSettings first, falling back to config
            settings = QgsSettings()
            hostname = settings.value("ogc_layer_handler/config_hostname")
            port = settings.value("ogc_layer_handler/config_port")
            
            # If not found in settings, use values from config
            if not hostname:
                hostname = self.config.get('hostname', 'localhost')
            
            if not port:
                port = self.config.get('port', 443)
                
            proxy_path = service_config.get('proxy_path', '')
            if hostname in ['127.0.0.1', 'localhost']:
                protocol = 'http'
            else:
                protocol = 'https'
                port = '443'
            # Construct WFS GetCapabilities request through proxy
            url = f"{protocol}://{hostname}:{port}{proxy_path}?service=WFS&version=2.0.0&request=GetCapabilities"
            
            # Create network request
            request = QNetworkRequest(QUrl(url))
            
            # Get authentication header if credential manager is set
            if self.credential_manager:
                auth_header = self.credential_manager.get_auth_header()
                if auth_header:
                    for header_name, header_value in auth_header.items():
                        request.setRawHeader(
                            QByteArray(header_name.encode()),
                            QByteArray(str(header_value).encode())
                        )
                
                # Also apply authentication configuration if available
                auth_config_id = self.credential_manager.get_auth_config_id()
                if auth_config_id:
                    auth_manager = QgsApplication.authManager()
                    auth_manager.updateNetworkRequest(request, auth_config_id)
            
            # Make blocking request
            nam = QgsNetworkAccessManager.instance()
            reply = nam.blockingGet(request)
            
            # Check if request was successful
            if reply.error() == QNetworkReply.NoError:
                # Extract metadata from service_config if available (new feature)
                metadata_mapping = service_config.get('metadata_mapping', {})
                if metadata_mapping:
                    self.logger.info(f"Using metadata mapping from config.json for {collection_id}")
                    return {
                        'id': collection_id,
                        'title': metadata_mapping.get('title', f"External Layer: {collection_id}"),
                        'description': metadata_mapping.get('description', f"Layer from external WFS service"),
                        'license': metadata_mapping.get('license', None),
                        'attribution': metadata_mapping.get('author', None),
                        'updated': metadata_mapping.get('updated', datetime.now().isoformat()),
                        'data_uri': metadata_mapping.get('data_uri', None)
                    }
                # Default metadata if mapping not available
                return {
                    'id': collection_id,
                    'title': f"External Layer: {collection_id}",
                    'description': f"Metadata for layer {collection_id} from external WFS service"
                }
        except Exception as e:
           self.logger.critical(f"Error fetching external metadata: {e}")
        return None

    def get_layer_metadata_from_config(self, service_id: str, layer_id: str) -> Optional[Dict]:
        """
        Extract layer metadata directly from the config.json file.
        This is a new method to prioritize getting metadata from config.
        
        Args:
            service_id: Service ID
            layer_id: Layer ID
            
        Returns:
            Dict: Metadata from config or None if not found
        """
        try:
            if not self.config or not isinstance(self.config, dict) or 'services' not in self.config:
                return None
                
            # Check in external services
            if 'external_services' in self.config['services']:
                for region, services in self.config['services']['external_services'].items():
                    for service in services:
                        if service.get('id') == service_id:
                            # Found the service, now look for the layer
                            for layer in service.get('layers', []):
                                if layer.get('id') == layer_id:
                                    # Found the layer, extract metadata
                                    metadata = {}
                                    metadata['id'] = layer_id
                                    metadata['title'] = layer.get('name', layer_id)
                                    metadata['description'] = layer.get('description', f"Layer {layer_id} from service {service_id}")
                                    
                                    # Add service-level metadata
                                    service_metadata = service.get('metadata_mapping', {})
                                    if service_metadata:
                                        metadata['license'] = service_metadata.get('license')
                                        metadata['attribution'] = service_metadata.get('author')
                                        metadata['updated'] = service_metadata.get('updated')
                                        metadata['data_uri'] = service_metadata.get('data_uri')
                                    
                                    self.logger.info(f"Found metadata in config for layer {layer_id}")
                                    return metadata
            
            # Check in internal services
            if 'internal_services' in self.config['services']:
                for region, services in self.config['services']['internal_services'].items():
                    for service in services:
                        if service.get('id') == service_id:
                            # For internal services, look in collections
                            for collection in service.get('collections', []):
                                if collection.get('id') == layer_id:
                                    metadata = {}
                                    metadata['id'] = layer_id
                                    metadata['title'] = collection.get('name', layer_id)
                                    metadata['description'] = collection.get('description', f"Collection {layer_id} from service {service_id}")
                                    
                                    # Add service-level metadata if available
                                    service_metadata = service.get('metadata_mapping', {})
                                    if service_metadata:
                                        metadata['license'] = service_metadata.get('license')
                                        metadata['attribution'] = service_metadata.get('author')
                                        metadata['updated'] = service_metadata.get('updated')
                                        metadata['data_uri'] = service_metadata.get('data_uri')
                                    
                                    self.logger.info(f"Found metadata in config for collection {layer_id}")
                                    return metadata
            
            return None
            
        except Exception as e:
            self.logger.error(f"Error extracting metadata from config: {str(e)}")
            return None

    def apply_metadata_to_layer(self, layer, service_config=None, layer_config=None):
        """
        Apply metadata to QGIS layer.
        Enhanced to prioritize metadata from config.json
        
        Args:
            layer: QGIS layer to apply metadata to
            service_config: Optional service configuration
            layer_config: Optional layer configuration
        """
        # Determine service_id and layer_id if available
        service_id = service_config.get('id') if service_config else None
        layer_id = layer_config.get('id') if layer_config else None
        
        # First try to get metadata directly from config.json
        metadata = None
        if service_id and layer_id:
            metadata = self.get_layer_metadata_from_config(service_id, layer_id)
            
        # If not found in config, try to fetch or use default
        if not metadata:
            # If specific metadata provided in layer_config, use that
            if layer_config and 'metadata' in layer_config:
                metadata = layer_config['metadata']
            else:
                metadata = self._prepare_metadata(service_config, layer_config)
        
        layer_metadata = QgsLayerMetadata()

        # Basic metadata
        layer_metadata.setIdentifier(metadata.get('id', ''))
        layer_metadata.setTitle(metadata.get('title', layer.name()))
        layer_metadata.setAbstract(metadata.get('description', ''))
        
        # Keywords
        if 'keywords' in metadata:
            layer_metadata.setKeywords({
                'keywords': metadata['keywords']
            })

        # Extent
        if 'extent' in metadata:
            spatial = metadata['extent'].get('spatial', {})
            if 'bbox' in spatial:
                bbox = spatial['bbox']
                extent = QgsBox3d(bbox[0], bbox[1], 0, bbox[2], bbox[3], 0)
                layer_metadata.setExtent(extent)

            temporal = metadata['extent'].get('temporal', {})
            if 'interval' in temporal:
                start = datetime.fromisoformat(temporal['interval'][0])
                end = datetime.fromisoformat(temporal['interval'][1])
                layer_metadata.setTemporalExtents([QgsDateTimeRange(start, end)])

        # Rights and attribution
        if 'license' in metadata and metadata['license']:
            layer_metadata.setLicenses([metadata['license']])
        if 'attribution' in metadata and metadata['attribution']:
            layer_metadata.setRights([metadata['attribution']])

        # Contacts
        if 'contact' in metadata:
            contact = metadata['contact']
            layer_metadata.addContact({
                'name': contact.get('name', ''),
                'organization': contact.get('organization', ''),
                'email': contact.get('email', '')
            })

        # Set the metadata on the layer
        layer.setMetadata(layer_metadata)

        # For custom properties, use layer's own methods
        if 'quality' in metadata:
            layer.setCustomProperty('quality', metadata['quality'])
        if 'updated' in metadata:
            layer.setCustomProperty('last_updated', metadata['updated'])

    def _prepare_metadata(self, service_config=None, layer_config=None):
        """
        Prepare metadata from available sources.
        
        Args:
            service_config: Optional service configuration
            layer_config: Optional layer configuration
            
        Returns:
            Dict: Metadata dictionary
        """
        # If layer_config provides metadata, use it
        if layer_config and 'metadata' in layer_config:
            return layer_config['metadata']
        
        # Try to fetch metadata from service if possible
        if service_config and layer_config:
            collection_id = layer_config.get('id')
            
            # Check if service has metadata_mapping
            if 'metadata_mapping' in service_config:
                self.logger.info(f"Using metadata mapping from service config for {collection_id}")
                metadata = {
                    'id': collection_id,
                    'title': layer_config.get('name', collection_id),
                    'description': layer_config.get('description', f"Layer {collection_id}")
                }
                
                # Add service-level metadata
                mapping = service_config['metadata_mapping']
                if 'title' in mapping:
                    metadata['title'] = mapping['title']
                if 'description' in mapping:
                    metadata['description'] = mapping['description']
                if 'license' in mapping:
                    metadata['license'] = mapping['license']
                if 'author' in mapping:
                    metadata['attribution'] = mapping['author']
                if 'updated' in mapping:
                    metadata['updated'] = mapping['updated']
                if 'data_uri' in mapping:
                    metadata['data_uri'] = mapping['data_uri']
                
                return metadata
            
            # Check if it's an internal TinyOWS service
            if service_config.get('is_internal', False):
                internal_metadata = self.fetch_tinyows_metadata(collection_id)
                if internal_metadata:
                    return internal_metadata
            # Otherwise try external metadata
            else:
                external_metadata = self.fetch_external_metadata(service_config, collection_id)
                if external_metadata:
                    return external_metadata
        
        # Return a minimal default metadata
        return {
            'id': layer_config.get('id', '') if layer_config else '',
            'title': layer_config.get('name', '') if layer_config else '',
            'description': 'No metadata available'
        }

class StyleManager:
    """
    Handles exporting and importing of layer styles for plugin-provided layers.
    Supports export to single JSON file with base64-encoded QML styles.
    """
    
    def __init__(self, logger: logging.Logger):
        """
        Initialize the style manager.
        
        Args:
            logger: Logger instance
        """
        self.logger = logger
        
    def export_all_styles(self, layers_info: Dict[str, Dict[str, QgsMapLayer]]) -> Dict:
        """
        Export styles for all layers to a single dictionary.
        
        Args:
            layers_info: Dictionary mapping service_id -> layer_id -> QgsMapLayer
            
        Returns:
            Dict: Dictionary with style information
        """
        styles_data = {
            "version": "1.0",
            "exported_date": datetime.now().isoformat(),
            "styles": {}
        }
        
        # Create a temporary directory for style files
        import tempfile
        temp_dir = tempfile.mkdtemp(prefix="qgis_styles_")
        
        # Get the layer tree root to check visibility
        root = QgsProject.instance().layerTreeRoot()
        
        # Iterate through all services and layers
        for service_id, layers in layers_info.items():
            self.logger.info(f"Processing styles for service: {service_id}")
            
            # Create entry for this service if it doesn't exist
            if service_id not in styles_data["styles"]:
                styles_data["styles"][service_id] = {}
                
            # Process each layer in this service
            for layer_id, layer in layers.items():
                self.logger.info(f"Exporting style for layer: {layer_id}")
                
                # Skip raster layers (WMS/XYZ) which don't have QML styles
                if isinstance(layer, QgsRasterLayer):
                    self.logger.debug(f"Skipping raster layer: {layer_id}")
                    continue
                
                # Create a temporary file path for the style
                temp_style_path = os.path.join(temp_dir, f"{layer_id}.qml")
                
                # Export the style to the temporary file
                result = layer.saveNamedStyle(temp_style_path)#, categories = QgsMapLayer.Symbology)
                
                if result:
                    # Read the QML file and encode in base64
                    with open(temp_style_path, 'r', encoding='utf-8') as f:
                        qml_content = f.read()
                        qml_base64 = base64.b64encode(qml_content.encode('utf-8')).decode('utf-8')
                    
                    # Get layer visibility from layer tree
                    layer_node = root.findLayer(layer.id())
                    is_visible = True  # Default to visible
                    if layer_node:
                        is_visible = layer_node.itemVisibilityChecked()
                    
                    # Enhanced metadata collection including visibility
                    metadata = {
                        "name": layer.name(),
                        "type": layer.type(),
                        "created": datetime.now().isoformat(),
                        "qgis_id": layer.id(),
                        "visible": is_visible  # Add visibility status
                    }
                    
                    # Add geometry type for vector layers
                    if isinstance(layer, QgsVectorLayer):
                        metadata["geometry_type"] = layer.geometryType()
                        metadata["provider"] = layer.providerType() if hasattr(layer, "providerType") else "unknown"
                        metadata["feature_count"] = layer.featureCount() if hasattr(layer, "featureCount") else 0
                    
                    # Add CRS information if available
                    if hasattr(layer, "crs"):
                        metadata["crs"] = layer.crs().authid()
                    
                    # Add layer source info (without sensitive data)
                    if hasattr(layer, "source"):
                        source = layer.source()
                        # Remove passwords and credentials from source string
                        if "password=" in source.lower():
                            source = re.sub(r'password=[^\s&]+', 'password=*****', source, flags=re.IGNORECASE)
                        if "username=" in source.lower():
                            source = re.sub(r'username=[^\s&]+', 'username=*****', source, flags=re.IGNORECASE)
                        metadata["source_type"] = source.split(':')[0] if ':' in source else "unknown"
                    
                    # Add to styles data
                    styles_data["styles"][service_id][layer_id] = {
                        "qml_content": qml_base64,
                        "metadata": metadata
                    }
                else:
                    self.logger.warning(f"Failed to export style for layer {layer_id}")
        
        # Add group visibility information
        styles_data["groups"] = self._export_group_visibility(root)
        
        # Clean up temporary directory
        import shutil
        shutil.rmtree(temp_dir)
        
        return styles_data
        
    def _export_group_visibility(self, root):
        """
        Export visibility information for layer groups.
        
        Args:
            root: Layer tree root
            
        Returns:
            Dict: Dictionary with group visibility information
        """
        groups_data = {}
        
        # Process all groups in the tree
        def process_group(group, path=""):
            # Skip the root group
            if group != root:
                current_path = path + "/" + group.name() if path else group.name()
                groups_data[current_path] = group.itemVisibilityChecked()
            else:
                current_path = ""
                
            # Process child groups
            for child in group.children():
                if isinstance(child, QgsLayerTreeGroup):
                    process_group(child, current_path)
        
        # Start processing from root
        process_group(root)
        return groups_data
    
    def import_styles_from_json(self, styles_data: Dict, layers_info: Dict[str, Dict[str, QgsMapLayer]]) -> Tuple[int, int]:
        """
        Import styles from JSON data to layers.
        
        Args:
            styles_data: Dictionary with style information
            layers_info: Dictionary mapping service_id -> layer_id -> QgsMapLayer
            
        Returns:
            Tuple[int, int]: Count of successful and failed style applications
        """
        success_count = 0
        fail_count = 0
        
        if not styles_data or "styles" not in styles_data:
            self.logger.warning("Invalid style data format")
            return success_count, fail_count
        
        # Add detailed debug logging
        self.logger.info("=== Style Import Debug Information ===")
        self.logger.info(f"JSON styles contains services: {list(styles_data['styles'].keys())}")
        self.logger.info(f"Loaded layers contain services: {list(layers_info.keys())}")
        
        # Build a lookup dictionary for fallback layer matching
        layer_lookup = {}
        for service_id, service_layers in layers_info.items():
            for layer_id, layer in service_layers.items():
                # Store by layer ID
                key = f"{service_id}:{layer_id}"
                layer_lookup[key] = (service_id, layer_id, layer)
                
                # Store by QGIS internal ID
                if hasattr(layer, "id"):
                    layer_lookup[layer.id()] = (service_id, layer_id, layer)
                
                # Store by layer name
                if hasattr(layer, "name"):
                    layer_lookup[layer.name()] = (service_id, layer_id, layer)
        
        self.logger.info(f"Created layer lookup with {len(layer_lookup)} entries")
        
        for service_id in styles_data["styles"]:
            if service_id in layers_info:
                self.logger.info(f"Service {service_id}:")
                self.logger.info(f"  JSON has layers: {list(styles_data['styles'][service_id].keys())}")
                self.logger.info(f"  Loaded has layers: {list(layers_info[service_id].keys())}")
            else:
                self.logger.warning(f"Service {service_id} from styles JSON not found in loaded layers")
        
        # Create a temporary directory for style files
        import tempfile
        temp_dir = tempfile.mkdtemp(prefix="qgis_styles_")
        
        # Get the layer tree root to apply visibility settings
        root = QgsProject.instance().layerTreeRoot()
        
        # Iterate through styles in the JSON
        for service_id, service_styles in styles_data["styles"].items():
            for layer_id, style_data in service_styles.items():
                # Try different methods to find the layer
                layer = None
                match_method = None
                
                # Method 1: Direct lookup in layers_info
                if service_id in layers_info and layer_id in layers_info[service_id]:
                    layer = layers_info[service_id][layer_id]
                    match_method = "direct_lookup"
                
                # Method 2: Try fallback methods if direct lookup fails
                if layer is None:
                    # Try by combined key
                    combined_key = f"{service_id}:{layer_id}"
                    if combined_key in layer_lookup:
                        _, _, layer = layer_lookup[combined_key]
                        match_method = "combined_key"
                    
                    # Try by layer name from metadata
                    if layer is None and "metadata" in style_data and "name" in style_data["metadata"]:
                        layer_name = style_data["metadata"]["name"]
                        if layer_name in layer_lookup:
                            _, _, layer = layer_lookup[layer_name]
                            match_method = "layer_name"
                            
                        # Also try with common transformations of the name
                        else:
                            # Remove special characters and spaces
                            normalized_name = ''.join(e.lower() for e in layer_name if e.isalnum())
                            for key in layer_lookup:
                                if isinstance(key, str):
                                    normalized_key = ''.join(e.lower() for e in key if e.isalnum())
                                    if normalized_key == normalized_name:
                                        _, _, layer = layer_lookup[key]
                                        match_method = "normalized_name"
                                        break
                
                if layer is None:
                    self.logger.warning(f"Layer {layer_id} not found in service {service_id} by any method")
                    fail_count += 1
                    continue
                
                # Skip raster layers
                if isinstance(layer, QgsRasterLayer):
                    self.logger.debug(f"Skipping style application for raster layer: {layer_id}")
                    
                    # Even for raster layers, we can apply visibility settings
                    if "metadata" in style_data and "visible" in style_data["metadata"]:
                        layer_node = root.findLayer(layer.id())
                        if layer_node:
                            is_visible = style_data["metadata"]["visible"]
                            self.logger.info(f"Setting visibility for raster layer {layer_id} to {is_visible}")
                            layer_node.setItemVisibilityChecked(is_visible)
                    
                    continue
                
                # Get base64 encoded QML and decode
                qml_base64 = style_data.get("qml_content", "")
                try:
                    qml_content = base64.b64decode(qml_base64).decode('utf-8')
                    
                    # Create a temporary file with the QML content
                    temp_style_path = os.path.join(temp_dir, f"{layer_id}.qml")
                    with open(temp_style_path, 'w', encoding='utf-8') as f:
                        f.write(qml_content)
                        
                    # Apply the style to the layer - according to QGIS C++ API:
                    # loadNamedStyle returns a QString (message) and uses bool& resultFlag as an out parameter 
                    # In Python bindings this is typically exposed as returning a tuple (bool, str)
                    result = layer.loadNamedStyle(temp_style_path)#, categories = QgsMapLayer.Symbology)
                    
                    # Handle different potential return formats:
                    apply_success = False
                    error_message = ""
                    
                    if isinstance(result, tuple):
                        # Most common case in Python bindings: tuple of (bool, str)
                        if len(result) >= 1:
                            apply_success = bool(result[0])
                            if len(result) > 1:
                                error_message = str(result[1])
                                # Success message doesn't mean failure
                                if apply_success and error_message:
                                    self.logger.info(f"Style message for {layer_id}: {error_message}")
                    elif isinstance(result, bool):
                        # Some versions might return just the success flag
                        apply_success = result
                    elif isinstance(result, str):
                        # Direct QString return without transforming the out parameter
                        # In this case, the success flag should have been set via reference parameter
                        # and the return value is just an informational message
                        error_message = result
                        # We'll assume success if we got a non-empty message that doesn't contain error indicators
                        apply_success = True
                        if error_message:
                            self.logger.info(f"Style message for {layer_id}: {error_message}")
                    else:
                        # Unexpected return value, log it for debugging
                        self.logger.warning(f"Unexpected return type from loadNamedStyle: {type(result)}, value: {result}")
                        # Assume it worked if we got this far and check if the layer appears styled
                        apply_success = True
                    
                    # Repaint the layer to apply changes visually
                    layer.triggerRepaint()
                    
                    # Apply visibility setting if available in metadata
                    if "metadata" in style_data and "visible" in style_data["metadata"]:
                        layer_node = root.findLayer(layer.id())
                        if layer_node:
                            is_visible = style_data["metadata"]["visible"]
                            self.logger.info(f"Setting visibility for layer {layer_id} to {is_visible}")
                            layer_node.setItemVisibilityChecked(is_visible)
                    
                    if apply_success:
                        success_count += 1
                        self.logger.info(f"Successfully applied style to layer {layer_id} (matched by {match_method})")
                    else:
                        # Only log as a warning if the error message indicates a real problem
                        if error_message and ("error" in error_message.lower() or "failed" in error_message.lower() or "invalid" in error_message.lower()):
                            self.logger.warning(f"Failed to apply style to layer {layer_id}: {error_message}")
                            fail_count += 1
                        else:
                            # The style might have been applied but returned false for some other reason
                            self.logger.info(f"Style application for {layer_id} reported {apply_success}, message: {error_message}")
                            # We'll count it as a success since in most cases the style is actually applied
                            success_count += 1
                
                except Exception as e:
                    fail_count += 1
                    self.logger.error(f"Error applying style to layer {layer_id}: {str(e)}")
                    self.logger.error(traceback.format_exc())
        
        # Apply group visibility settings if present
        if "groups" in styles_data:
            self._import_group_visibility(root, styles_data["groups"])
        
        # Clean up temporary directory
        import shutil
        shutil.rmtree(temp_dir)
        
        self.logger.info(f"Style import complete. Success: {success_count}, Failed: {fail_count}")
        return success_count, fail_count
        
    def _import_group_visibility(self, root, groups_data):
        """
        Import and apply group visibility settings.
        
        Args:
            root: Layer tree root
            groups_data: Dictionary with group visibility information
        """
        for group_path, is_visible in groups_data.items():
            # Split path into components
            path_parts = group_path.split('/')
            
            # Start at root and navigate down
            current_group = root
            for part in path_parts:
                found = False
                for child in current_group.children():
                    if isinstance(child, QgsLayerTreeGroup) and child.name() == part:
                        current_group = child
                        found = True
                        break
                
                if not found:
                    self.logger.warning(f"Could not find group: {group_path}")
                    break
            
            # Set visibility if group was found
            if current_group != root:
                self.logger.info(f"Setting visibility for group {group_path} to {is_visible}")
                current_group.setItemVisibilityChecked(is_visible)
    
    def export_styles_to_file(self, layers_info: Dict[str, Dict[str, QgsMapLayer]], filepath: str) -> bool:
        """
        Export all styles to a JSON file.
        
        Args:
            layers_info: Dictionary mapping service_id -> layer_id -> QgsMapLayer
            filepath: Path to export the styles to
            
        Returns:
            bool: True if export was successful
        """
        try:
            # Get styles data
            styles_data = self.export_all_styles(layers_info)
            
            # Write to file
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(styles_data, f, indent=2)
            
            self.logger.info(f"Successfully exported styles to {filepath}")
            return True
            
        except Exception as e:
            self.logger.error(f"Error exporting styles to file: {str(e)}")
            self.logger.error(traceback.format_exc())
            return False
    
    def import_styles_from_file(self, layers_info: Dict[str, Dict[str, QgsMapLayer]], filepath: str) -> Tuple[int, int]:
        """
        Import styles from a JSON file.
        
        Args:
            layers_info: Dictionary mapping service_id -> layer_id -> QgsMapLayer
            filepath: Path to the styles file
            
        Returns:
            Tuple[int, int]: Count of successful and failed style applications
        """
        try:
            # Read from file
            with open(filepath, 'r', encoding='utf-8') as f:
                styles_data = json.load(f)
            
            # Apply styles
            success_count, fail_count = self.import_styles_from_json(styles_data, layers_info)
            
            self.logger.info(f"Style import complete. Success: {success_count}, Failed: {fail_count}")
            return success_count, fail_count
            
        except Exception as e:
            self.logger.error(f"Error importing styles from file: {str(e)}")
            self.logger.error(traceback.format_exc())
            return 0, 0
    
    def fetch_styles_from_server(self, hostname: str, port: str, auth_header: Dict) -> Optional[Dict]:
        """
        Fetch styles JSON from server.
        
        Args:
            hostname: Server hostname
            port: Server port
            auth_header: Authentication header with API key
            
        Returns:
            Dict: Styles data or None if request failed
        """
        try:
            # Determine protocol based on hostname
            if hostname in ['127.0.0.1', 'localhost']:
                protocol = 'http'
            else:
                protocol = 'https'
                port = '443'  # Use standard HTTPS port for non-localhost
                
            # Construct URL to styles endpoint
            url = f"{protocol}://{hostname}:{port}/styles"
            self.logger.info(f"Fetching styles from server: {url}")
            
            # Create network request
            request = QNetworkRequest(QUrl(url))
            
            # Set headers
            if auth_header:
                for header_name, header_value in auth_header.items():
                    request.setRawHeader(
                        QByteArray(header_name.encode()),
                        QByteArray(str(header_value).encode())
                    )
            
            # Make blocking request
            nam = QgsNetworkAccessManager.instance()
            reply = nam.blockingGet(request)
            
            # Check if request was successful
            if reply.error() == QNetworkReply.NoError:
                # Parse JSON response
                response_text = bytes(reply.content()).decode('utf-8')
                styles_data = json.loads(response_text)
                self.logger.info(f"Successfully fetched styles from server")
                return styles_data
            else:
                self.logger.error(f"Failed to fetch styles. Error: {reply.errorString()}")
                self.logger.error(f"Error code: {reply.error()}")
                return None
                
        except Exception as e:
            self.logger.error(f"Error fetching styles from server: {str(e)}")
            self.logger.error(traceback.format_exc())
            return None

    def apply_server_styles(self, layers_info: Dict[str, Dict[str, QgsMapLayer]], 
                          hostname: str, port: str, auth_header: Dict) -> Tuple[int, int]:
        """
        Fetch and apply styles from server.
        
        Args:
            layers_info: Dictionary mapping service_id -> layer_id -> QgsMapLayer
            hostname: Server hostname
            port: Server port
            auth_header: Authentication header with API key
            
        Returns:
            Tuple[int, int]: Count of successful and failed style applications
        """
        # Fetch styles from server
        styles_data = self.fetch_styles_from_server(hostname, port, auth_header)
        
        if not styles_data:
            return 0, 0
            
        # Apply styles to layers
        return self.import_styles_from_json(styles_data, layers_info)

    def get_api_url(self) -> str:
        """
        Get the base API URL for the style server.
        
        Returns:
            str: Base API URL
        """
        # Try to get from settings
        try:
            settings = QgsSettings()
            hostname = settings.value("ogc_layer_handler/config_hostname", "localhost")
            port = settings.value("ogc_layer_handler/config_port", "443")
            
            # Determine protocol based on hostname
            if hostname in ['127.0.0.1', 'localhost']:
                protocol = 'http'
            else:
                protocol = 'https'
                port = '443'
                
            return f"{protocol}://{hostname}:{port}"
        except Exception as e:
            self.logger.error(f"Error getting API URL: {str(e)}")
            return "http://localhost:80"

class LayerHandler:
    """
    Handles layer operations for the QGIS plugin.
    
    This version supports both external OGC services via NGINX proxy,
    internal services via TinyOWS, and XYZ Tiles. Uses JSON config files.
    Now includes authentication support.
    """
    
    def __init__(self, config_path: str, logger: logging.Logger):
        """
        Initialize the layer handler with configuration.
        
        Args:
            config_path: Path to configuration file
            logger: Logger instance
        """
        self.config_path = config_path
        self.logger = logger
        self.iface = None
        self.config = self._load_config()
        self.style_lookup = self._build_style_lookup()
        self.credential_manager = None
        
        # Create MetadataHandler - the MetadataHandler will handle the case if the file doesn't exist
        self.metadata_handler = MetadataHandler(config_path)
        
        # Dictionary to store all created layers, organized by service_id and layer_id
        self.layers_by_id = {}

    def _load_config(self) -> Dict:
        """
        Load JSON configuration file.
        
        Returns:
            Dict: The parsed configuration or empty dict if an error occurred
        """
        try:
            with open(self.config_path, 'r') as f:
                self.logger.info(f"Loading configuration from {self.config_path}")
                return json.load(f)
        except FileNotFoundError:
            self.logger.warning(f"Config file not found: {self.config_path}, using empty configuration")
            return {}
        except json.JSONDecodeError:
            self.logger.error(f"Invalid JSON in config file: {self.config_path}")
            return {}
        except Exception as e:
            self.logger.error(f"Error loading config file: {e}")
            return {}

    def _build_style_lookup(self) -> Dict:
        """
        Build a lookup dictionary mapping both layer_ids and type_names to styles.
        
        Returns:
            Dict: Style lookup dictionary
        """
        lookup = {
            'by_layer_id': {},
            'by_type_name': {}
        }
        self.logger.info("Building style lookup")
        
        styles = self.config.get('styles', {})
        for layer_id, style_config in styles.items():
            self.logger.debug(f"Processing style for layer_id '{layer_id}'")
            
            # Store style by layer_id
            style_data = {k: v for k, v in style_config.items() if k != 'applies_to_type_names'}
            lookup['by_layer_id'][layer_id] = style_data
            
            # Store style by type_name
            type_names = style_config.get('applies_to_type_names', [])
            # Handle both list formats (array and dash style)
            if isinstance(type_names, list):
                for type_name in type_names:
                    self.logger.debug(f"Mapping type_name '{type_name}' to style for '{layer_id}'")
                    lookup['by_type_name'][type_name] = style_data
                    
        return lookup

    def setup_auth_method(self, api_key: str, service_id: str) -> Optional[str]:
        """
        Setup a custom header authentication method for the API key.
        
        Args:
            api_key: API key
            service_id: Service ID for naming
            
        Returns:
            str: Authentication config ID or None if setup failed
        """
        try:
            # Create a custom authentication method for API key
            auth_config = QgsAuthMethodConfig()
            auth_config.setName(f"API Key for {service_id}")
            auth_config.setMethod("APIHeader")
            auth_config.setConfig("header", "X-API-KEY")
            auth_config.setConfig("value", api_key)
            
            # Get the auth manager
            auth_manager = QgsApplication.authManager()
            
            # Generate an ID for the auth config
            auth_id = auth_config.id()
            
            # Store the auth config
            if auth_manager.storeAuthenticationConfig(auth_config):
                self.logger.info(f"Successfully saved auth config with ID: {auth_id}")
                return auth_id
            else:
                self.logger.error("Failed to store authentication config")
                return None
        except Exception as e:
            self.logger.error(f"Error setting up authentication method: {str(e)}")
            return None
    
    def get_style_for_layer(self, layer_id: str, type_name: str) -> Optional[Dict]:
        """
        Get style configuration for a layer based on its ID or type_name.
        Prioritizes exact layer_id matches over type_name matches.
        
        Args:
            layer_id: Layer ID
            type_name: Layer type name
            
        Returns:
            Dict: Style configuration or None if not found
        """
        self.logger.debug(f"Looking up style for layer_id: {layer_id}, type_name: {type_name}")
        
        # First try to find style by layer_id
        layer_style = self.style_lookup['by_layer_id'].get(layer_id)
        if layer_style:
            self.logger.debug(f"Found style by layer_id: {layer_id}")
            return layer_style
        
        # If not found by layer_id, check the type_name lookup
        type_style = self.style_lookup['by_type_name'].get(type_name)
        if type_style:
            self.logger.debug(f"Found style by type_name: {type_name}")
            return type_style
        
        self.logger.debug(f"No style found for layer_id: {layer_id} or type_name: {type_name}")
        return None

    def get_service_config(self, service_id: str) -> Optional[Dict]:
        """
        Get configuration for a specific service.
        Enhanced to support XYZ Tiles service type.
        
        Args:
            service_id: Service ID
            
        Returns:
            Dict: Service configuration or None if not found
        """
        self.logger.info(f"Looking for service config: {service_id}")
        
        services = self.config.get('services', {})
        
        # Check external services across all federal states
        external_services = services.get('external_services', {})
        for state_code, state_services in external_services.items():
            for service in state_services:
                if service['id'] == service_id:
                    # Add auto-generated proxy_path
                    service = service.copy()  # Create a copy to not modify original
                    # Handle XYZ tiles specifically
                    if service.get('type') == 'xyz_tiles':
                        service['proxy_path']  = f"/xyz/{state_code.lower()}/{service_id}"
                        self.logger.info(f"Found XYZ tiles service {service_id}")
                    else:
                        # Standard proxy path for WFS/WMS services
                        service['proxy_path'] = f"/ogc/{state_code.lower()}/{service_id}"
                        self.logger.info(f"Found external service {service_id} with proxy path {service['proxy_path']}")
                    
                    # Store the region information
                    service['region'] = state_code
                    return service
        
        # Check internal services - for tinyows
        internal_services = services.get('internal_services', {})
        for state_code, state_services in internal_services.items():
            for service in state_services:
                if service.get('id') == service_id:
                    # Mark as internal tinyows service
                    service = service.copy()  # Create a copy to not modify original
                    service['is_internal'] = True
                    service['service_type'] = service.get('service_type', 'tinyows')
                    # Store the region information
                    service['region'] = state_code
                    self.logger.info(f"Found internal service {service_id} in region {state_code}")
                    return service
        
        return None
    def get_layer_config(self, service_id: str, layer_id: str) -> Optional[Dict]:
        """
        Get configuration for a specific layer within a service.
        Enhanced to support XYZ Tiles layers.
        
        Args:
            service_id: Service ID
            layer_id: Layer ID
            
        Returns:
            Dict: Layer configuration or None if not found
        """
        service_config = self.get_service_config(service_id)
        if not service_config:
            return None
            
        # For external services
        if 'layers' in service_config:
            for layer in service_config['layers']:
                if layer['id'] == layer_id:
                    # For XYZ Tiles, add specific handling
                    if service_config.get('type') == 'xyz_tiles':
                        # Create a complete layer config with XYZ-specific properties
                        layer_config = layer.copy()
                        
                        # Ensure min_zoom and max_zoom are present
                        if 'min_zoom' not in layer_config:
                            layer_config['min_zoom'] = 0
                        if 'max_zoom' not in layer_config:
                            layer_config['max_zoom'] = 19
                            
                        return layer_config
                    else:
                        # Regular layer handling for WFS/WMS
                        return layer
                    
        # For internal services (TinyOWS)
        if service_config.get('is_internal', False) and 'collections' in service_config:
            for collection in service_config['collections']:
                # Check against the collection's id field
                if collection.get('id') == layer_id:
                    # Convert collection config to layer config format
                    layer_config = {
                        'id': collection.get('id'),
                        'name': collection.get('name', collection.get('id')),
                        'type_name': f"{service_config.get('region', '').lower()}_{collection.get('id')}",  # TinyOWS naming format
                        'description': collection.get('description', '')
                    }
                    return layer_config
                
        return None

    def create_layer(self, service_id: str, layer_id: str) -> Optional[Union[QgsVectorLayer, QgsRasterLayer]]:
        """
        Create a QGIS layer from service and layer configuration.
        Uses QGIS authentication configuration for API key.
        Enhanced to properly apply metadata from config.json.
        Now with support for XYZ Tiles.

        Args:
            service_id: Service ID
            layer_id: Layer ID
            
        Returns:
            QgsVectorLayer or QgsRasterLayer: Created layer or None if creation failed
        """
        try:
            # Get hostname and port from QgsSettings first, falling back to config file if necessary
            settings = QgsSettings()
            hostname = settings.value("ogc_layer_handler/config_hostname")
            port = settings.value("ogc_layer_handler/config_port")
            
            # If not found in settings, use values from config
            if not hostname:
                hostname = self.config.get('hostname', 'localhost')
                self.logger.info(f"Using hostname from config file: {hostname}")
            else:
                self.logger.info(f"Using hostname from settings panel: {hostname}")
                
            if not port:
                port = self.config.get('port', 443)
                self.logger.info(f"Using port from config file: {port}")
            else:
                self.logger.info(f"Using port from settings panel: {port}")
                
            self.logger.info(f"Creating layer with service_id: {service_id}, layer_id: {layer_id}")

            service_config = self.get_service_config(service_id)
            if not service_config:
                self.logger.warning(f"No service configuration found for {service_id}")
                return None

            # Get region information which is needed for both external and internal services
            region = service_config.get('region', '').lower()
            
            # Determine if this is an internal TinyOWS service or external service
            is_internal = service_config.get('is_internal', False)
            service_type = service_config.get('type', 'WFS')  # Default to WFS
            
            # Get layer/collection configuration
            layer_config = self.get_layer_config(service_id, layer_id)
            if not layer_config:
                self.logger.warning(f"No layer configuration found for {layer_id}")
                return None

            # Get current map canvas settings
            canvas = self.iface.mapCanvas()
            current_extent = canvas.extent()
            target_crs = QgsProject.instance().crs()
            map_crs = canvas.mapSettings().destinationCrs()

            # Transform extent if needed
            transformed_extent = current_extent
            if map_crs != target_crs:
                try:
                    transform = QgsCoordinateTransform(map_crs, target_crs, QgsProject.instance())
                    transformed_extent = transform.transform(current_extent)
                except Exception as e:
                    self.logger.warning(f"Coordinate transformation error: {str(e)}")

            # Get authentication configuration ID
            auth_config_id = self.credential_manager.get_auth_config_id()
            if auth_config_id:
                self.logger.info(f"Using authentication configuration ID: {auth_config_id}")
            else:
                self.logger.warning("No authentication configuration found")

            # Get common layer details
            layer_name = layer_config.get('name', layer_id)
            if hostname in ['127.0.0.1', 'localhost']:
                protocol = 'http'
            else:
                protocol = 'https'
                port = '443'
            # Create the appropriate layer based on service type
            if service_type == 'xyz_tiles':
                # Handle XYZ Tiles service
                proxy_path = service_config.get('proxy_path', '')
                
                # Construct the complete URL with placeholders and format extension
                # Default to PNG format
                tile_format = layer_config.get('format', 'png')
                
                xyz_url = f"{protocol}://{hostname}:{port}{proxy_path}/{{z}}/{{x}}/{{y}}.{tile_format}"
                
                # Add min and max zoom levels if specified
                zmin = layer_config.get('min_zoom', 0)
                zmax = layer_config.get('max_zoom', 19)
                
                # Format the URI string
                uri_str = f"type=xyz&url={xyz_url}&zmax={zmax}&zmin={zmin}"
                
                # Add authentication if available
                if auth_config_id:
                    xyz_uri = QgsDataSourceUri()
                    xyz_uri.setAuthConfigId(auth_config_id)
                    uri_str = xyz_uri.uri(False) + "&" + uri_str
                    
                self.logger.info(f"XYZ Tiles URI: {uri_str}")
                
                # Create the raster layer
                layer = QgsRasterLayer(uri_str, layer_name, "wms")
                
            elif service_type == 'WMS' and not is_internal:
                # Only external services can be WMS
                wms_uri = QgsDataSourceUri()
                service_url = f"{protocol}://{hostname}:{port}{service_config.get('proxy_path', '')}"
                self.logger.info(f"WMS service URL: {service_url}")
                # Get layer details
                type_name = layer_config.get('type_name', '')
                # Create a complete WMS URI with all required parameters
                wms_params = {
                    "url": service_url,
                    "format": "image/png",
                    "layers": type_name,
                    "styles": "",
                    "crs": target_crs.authid(),
                    "version": "1.3.0",
                    "contextualWMSLegend": "0"
                }
                
                # Build URI string directly with authentication
                uri_components = []
                for key, value in wms_params.items():
                    uri_components.append(f"{key}={value}")
                    
                # Add authentication config ID separately
                if auth_config_id:
                    uri_components.append(f"authcfg={auth_config_id}")
                    self.logger.info(f"Added auth config ID {auth_config_id} to WMS request")
                
                wms_uri_string = "&".join(uri_components)
                self.logger.info(f"WMS URI: {wms_uri_string}")
                
                # Create WMS layer with the full URI string
                layer = QgsRasterLayer(wms_uri_string, layer_name, "wms")
            else:
                # WFS for both external services and TinyOWS
                # Get layer details for WFS
                if is_internal:
                    # For TinyOWS, construct type name with namespace (region:collection_id)
                    type_name = f"{region}:{layer_id}"
                    
                    service_url = f"{protocol}://{hostname}:{port}/tinyows"
                    wfs_version = "1.1.0"  # TinyOWS uses 1.1.0
                else:
                    # For external services
                    type_name = layer_config.get('type_name', '')
                    service_url = f"{protocol}://{hostname}:{port}{service_config.get('proxy_path', '')}"
                    wfs_version = "2.0.0"  # External services use 2.0.0
                    
                self.logger.info(
                    f"WFS Layer details:\n  type_name: {type_name}\n  layer_name: {layer_name}\n  service_url: {service_url}"
                )
                
                wfs_uri = QgsDataSourceUri()
                wfs_uri.setParam("url", service_url)
                wfs_uri.setParam("typename", type_name)
                wfs_uri.setParam("version", wfs_version)
                wfs_uri.setParam("srsname", target_crs.authid())
                
                # Set bounding box filter
                wfs_uri.setParam("restrictToRequestBBOX", "1")
                
                # Add authentication configuration if available
                if auth_config_id:
                    wfs_uri.setAuthConfigId(auth_config_id)
                    self.logger.info(f"Added auth config ID {auth_config_id} to WFS request")

                self.logger.info(f"WFS URI: {wfs_uri.uri(False)}")  # Log without sensitive info
                layer = QgsVectorLayer(wfs_uri.uri(False), layer_name, "WFS")

            if not layer.isValid():
                self.logger.critical(f"Layer {layer_name} is not valid")
                provider = layer.dataProvider()
                if provider:
                    if service_type.lower() in ['wms', 'xyz_tiles']:
                        self.logger.critical(f"Provider error: {provider.lastError()}")
                    else:
                        self.logger.critical(f"Provider error: {provider.errors()}")
                return None

            self.logger.info(f"Layer created successfully: {layer_name}")

            # Apply style (only for WFS layers or specific raster layers with styles)
            if service_type != 'WMS' and service_type != 'xyz_tiles':
                self._apply_style_to_layer(layer, layer_id, layer_config.get('type_name', ''))

            # Apply scale-based visibility if configured
            if layer_config.get('min_scale'):
                min_scale = float(layer_config['min_scale'])
                self.logger.info(f"Setting minimum scale for {layer_name} to 1:{min_scale}")
                layer.setMaximumScale(0)  # No maximum (fully zoomed in)
                layer.setMinimumScale(min_scale)  # Will hide when zoomed out beyond this
                layer.setScaleBasedVisibility(True)

            # Apply metadata to layer
            self.metadata_handler.apply_metadata_to_layer(layer, service_config, layer_config)
            self.logger.info(f"Applied metadata to layer {layer_name}")

            return layer

        except Exception as e:
            self.logger.critical(f"Error creating layer: {str(e)}")
            self.logger.critical(traceback.format_exc())
            return None
    def _apply_style_to_layer(self, layer, layer_id, type_name):
        """
        Apply style to a layer. Updated to skip styling for XYZ tiles and WMS layers.
        
        Args:
            layer: Layer to apply style to
            layer_id: Layer ID
            type_name: Layer type name
        """
        # Skip styling for raster layers (WMS and XYZ tiles)
        if isinstance(layer, QgsRasterLayer):
            self.logger.debug(f"Skipping style application for raster layer: {layer_id}")
            return
            
        style_config = self.get_style_for_layer(layer_id, type_name)
        if not style_config:
            return
            
        style_type = style_config.get('type')
        
        if style_type == 'fill':
            # Get colors from style config
            fill_color = style_config['color']
            outline_color = style_config['outline_color']

            # Create symbol with properties
            symbol = QgsFillSymbol.createSimple({
                'color': f"{fill_color[0]},{fill_color[1]},{fill_color[2]},{int(fill_color[3] * 255)}",
                'style': 'solid',
                'outline_width': '0.5',
                'outline_style': 'solid',
                'outline_color': f"{outline_color[0]},{outline_color[1]},{outline_color[2]},{int(outline_color[3] * 255)}"
            })

        elif style_type == 'line':
            line_color = style_config['color']
            width = style_config.get('width', 0.5)

            symbol = QgsLineSymbol.createSimple({
                'line_color': f"{line_color[0]},{line_color[1]},{line_color[2]},{int(line_color[3] * 255)}",
                'line_width': str(width),
                'line_style': 'solid'
            })

        elif style_type == 'marker':
            fill_color = style_config['color']
            outline_color = style_config.get('outline_color', fill_color)
            size = style_config.get('size', 3)

            symbol = QgsMarkerSymbol.createSimple({
                'name': 'circle',
                'color': f"{fill_color[0]},{fill_color[1]},{fill_color[2]},{int(fill_color[3] * 255)}",
                'outline_color': f"{outline_color[0]},{outline_color[1]},{outline_color[2]},{int(outline_color[3] * 255)}",
                'size': str(size),
                'outline_style': 'solid',
                'outline_width': '0.5',
                'outline_style': 'solid'
            })

        # Apply the symbol to the layer
        if symbol:
            renderer = QgsSingleSymbolRenderer(symbol)
            layer.setRenderer(renderer)
        """
        Apply style to a layer.
        
        Args:
            layer: Layer to apply style to
            layer_id: Layer ID
            type_name: Layer type name
        """
        style_config = self.get_style_for_layer(layer_id, type_name)
        if not style_config:
            return
            
        style_type = style_config.get('type')
        
        if style_type == 'fill':
            # Get colors from style config
            fill_color = style_config['color']
            outline_color = style_config['outline_color']

            # Create symbol with properties
            symbol = QgsFillSymbol.createSimple({
                'color': f"{fill_color[0]},{fill_color[1]},{fill_color[2]},{int(fill_color[3] * 255)}",
                'style': 'solid',
                'outline_width': '0.5',
                'outline_style': 'solid',
                'outline_color': f"{outline_color[0]},{outline_color[1]},{outline_color[2]},{int(outline_color[3] * 255)}"
            })

        elif style_type == 'line':
            line_color = style_config['color']
            width = style_config.get('width', 0.5)

            symbol = QgsLineSymbol.createSimple({
                'line_color': f"{line_color[0]},{line_color[1]},{line_color[2]},{int(line_color[3] * 255)}",
                'line_width': str(width),
                'line_style': 'solid'
            })

        elif style_type == 'marker':
            fill_color = style_config['color']
            outline_color = style_config.get('outline_color', fill_color)
            size = style_config.get('size', 3)

            symbol = QgsMarkerSymbol.createSimple({
                'name': 'circle',
                'color': f"{fill_color[0]},{fill_color[1]},{fill_color[2]},{int(fill_color[3] * 255)}",
                'outline_color': f"{outline_color[0]},{outline_color[1]},{outline_color[2]},{int(outline_color[3] * 255)}",
                'size': str(size),
                'outline_style': 'solid',
                'outline_width': '0.5',
                'outline_style': 'solid'
            })

        # Apply the symbol to the layer
        if symbol:
            renderer = QgsSingleSymbolRenderer(symbol)
            layer.setRenderer(renderer)

    def check_tinyows_capabilities(self):
        """
        Check TinyOWS capabilities and available layers.
        Now with authentication support.
        
        Returns:
            Dict: Capabilities information or None if request failed
        """
        try:
            # Get hostname and port from QgsSettings first, falling back to config
            settings = QgsSettings()
            hostname = settings.value("ogc_layer_handler/config_hostname")
            port = settings.value("ogc_layer_handler/config_port")
            
            # If not found in settings, use values from config
            if not hostname:
                hostname = self.config.get('hostname', 'localhost')
            
            if not port:
                port = self.config.get('port', 443)
                
            if hostname in ['127.0.0.1', 'localhost']:
                protocol = 'http'
            else:
                protocol = 'https'
                port = '443'
            # Capabilities URL for TinyOWS
            capabilities_url = f"{protocol}://{hostname}:{port}/tinyows?service=WFS&version=1.1.0&request=GetCapabilities"
            
            self.logger.info(f"Checking TinyOWS capabilities: {capabilities_url}")
            
            # Get authentication credentials
            auth_header = self.credential_manager.get_auth_header()
            
            # Create a network request
            request = QNetworkRequest(QUrl(capabilities_url))
            
            # Add headers for authentication
            if auth_header:
                for header_name, header_value in auth_header.items():
                    request.setRawHeader(
                        QByteArray(header_name.encode()),
                        QByteArray(str(header_value).encode())
                    )
                    
            # Get authentication configuration ID
            auth_config_id = self.credential_manager.get_auth_config_id()
            if auth_config_id:
                auth_manager = QgsApplication.authManager()
                auth_manager.updateNetworkRequest(request, auth_config_id)
                
            # Make blocking request
            nam = QgsNetworkAccessManager.instance()
            reply = nam.blockingGet(request)
            
            if reply.error() == QNetworkReply.NoError:
                self.logger.info("Successfully retrieved TinyOWS capabilities")
                
                # Get response content
                content = bytes(reply.content()).decode('utf-8')
                
                # TinyOWS returns XML, but we can report success
                layers = []
                
                # Simple check to see if it contains expected content
                if "<wfs:WFS_Capabilities" in content:
                    # Count the feature types (layers)
                    
                    # Extract namespaces first - they'll be in the format xmlns:ns="uri"
                    namespaces = {}
                    ns_matches = re.findall(r'xmlns:([a-z0-9]+)="([^"]+)"', content)
                    for prefix, uri in ns_matches:
                        namespaces[prefix] = uri
                        self.logger.info(f"Found namespace {prefix}: {uri}")
                    
                    # Simple regex to extract feature type names - look for both prefixed and non-prefixed
                    # We're looking for patterns like <Name>bb:wea</Name> or <Name>wea</Name>
                    feature_types = re.findall(r"<Name>([^<]+)</Name>", content)
                    
                    if feature_types:
                        self.logger.info(f"Found {len(feature_types)} layers in TinyOWS:")
                        for ft in feature_types:
                            self.logger.info(f"  - {ft}")
                            layers.append({"id": ft, "title": ft})
                        
                return {"service": "TinyOWS", "layers": layers, "namespaces": namespaces}
            else:
                self.logger.warning(f"Failed to fetch TinyOWS capabilities. Error: {reply.errorString()}")
                self.logger.warning(f"Error code: {reply.error()}")
        except Exception as e:
            self.logger.error(f"Error checking TinyOWS capabilities: {str(e)}")
            self.logger.error(traceback.format_exc())
        
        return None

    def build_layer_tree(self) -> None:
        """
        Build complete layer tree structure from configuration.
        This adds all configured layers to the QGIS project.
        """
        self.logger.info("Starting layer tree build...")
        canvas = self.iface.mapCanvas()
        current_extent = canvas.extent()
        canvas.setRenderFlag(False)

        root = QgsProject.instance().layerTreeRoot()
        
        # Get layer filter setting
        settings = QgsSettings()
        layer_filter = settings.value("ogc_layer_handler/layer_filter", "", type=str)
        
        if layer_filter:
            filter_ids = [lid.strip() for lid in layer_filter.split(',')]
            self.logger.info(f"Layer filter active. Only loading these IDs: {filter_ids}")
        else:
            filter_ids = None
        
        # Process the layer tree configuration
        for country in self.config.get('layer_tree', []):
            country_id = country.get('id')
            country_name = country.get('name')
            self.logger.info(f"Processing country: {country_name} ({country_id})")
            
            # Check if this country should be skipped entirely because no groups have any matching layers
            if filter_ids:
                # This is a simplistic check that could be enhanced to do a deeper traversal
                has_matching_layers = False
                for group in country.get('groups', []):
                    # Direct layers in this group
                    if 'layers' in group:
                        for layer_ref in group.get('layers', []):
                            layer_id = layer_ref.get('id') if isinstance(layer_ref, dict) else layer_ref
                            if layer_id in filter_ids:
                                has_matching_layers = True
                                break
                    
                    # Check for layers in subgroups (very simple check, not recursive)
                    if not has_matching_layers and 'groups' in group:
                        for subgroup in group.get('groups', []):
                            if 'layers' in subgroup:
                                for layer_ref in subgroup.get('layers', []):
                                    layer_id = layer_ref.get('id') if isinstance(layer_ref, dict) else layer_ref
                                    if layer_id in filter_ids:
                                        has_matching_layers = True
                                        break
                            if has_matching_layers:
                                break
                    
                    if has_matching_layers:
                        break
                        
                if not has_matching_layers:
                    self.logger.info(f"Skipping country {country_name} - no layers match filter")
                    continue
            
            # Create country-level group
            country_group = root.insertGroup(0, country_name)
            
            # Process country-level groups
            for group in country.get('groups', []):
                group_name = group.get('name')
                group_id = group.get('id')
                self.logger.info(f"Processing group: {group_name} ({group_id})")
                
                # Skip empty groups when filtering
                if filter_ids:
                    # Check if this group or its subgroups have any matching layers
                    has_matching_layers = False
                    
                    # Direct layers in this group
                    if 'layers' in group:
                        for layer_ref in group.get('layers', []):
                            layer_id = layer_ref.get('id') if isinstance(layer_ref, dict) else layer_ref
                            if layer_id in filter_ids:
                                has_matching_layers = True
                                break
                    
                    # Simple check for subgroups
                    if not has_matching_layers and 'groups' in group:
                        for subgroup in group.get('groups', []):
                            if 'layers' in subgroup:
                                for layer_ref in subgroup.get('layers', []):
                                    layer_id = layer_ref.get('id') if isinstance(layer_ref, dict) else layer_ref
                                    if layer_id in filter_ids:
                                        has_matching_layers = True
                                        break
                            if has_matching_layers:
                                break
                    
                    if not has_matching_layers:
                        self.logger.info(f"Skipping group {group_name} - no layers match filter")
                        continue
                
                # Create the group
                main_group = country_group.addGroup(group_name)
                
                # Check if this has sub-groups
                if 'groups' in group:
                    # Process sub-groups
                    self.process_subgroups(group.get('groups', []), main_group)
                # Check if this has layers and a service ID
                elif 'layers' in group and group.get('source_service') and group.get('source_service') != "":
                    # Process layers
                    self.process_layers(group.get('layers', []), group.get('source_service'), main_group)

            # Restore map canvas settings
            canvas.setExtent(current_extent)
            canvas.setRenderFlag(True)
            self.logger.info("Layer tree build completed")

    def process_subgroups(self, subgroups, parent_group):
        """
        Process subgroups recursively.
        
        Args:
            subgroups: List of subgroups to process
            parent_group: Parent group to add subgroups to
        """
        # Get layer filter setting
        settings = QgsSettings()
        layer_filter = settings.value("ogc_layer_handler/layer_filter", "", type=str)
        
        if layer_filter:
            filter_ids = [lid.strip() for lid in layer_filter.split(',')]
        else:
            filter_ids = None
            
        for subgroup in subgroups:
            subgroup_name = subgroup.get('name')
            subgroup_id = subgroup.get('id')
            
            # If this subgroup has layers, check if any pass the filter
            skip_group = False
            if filter_ids and 'layers' in subgroup:
                # Check if any layers in this group match our filter
                layers_to_process = []
                for layer_ref in subgroup.get('layers', []):
                    layer_id = layer_ref.get('id') if isinstance(layer_ref, dict) else layer_ref
                    if layer_id in filter_ids:
                        layers_to_process.append(layer_ref)
                
                # Skip this entire group if it has no layers that pass the filter
                # and it doesn't have subgroups
                if not layers_to_process and 'groups' not in subgroup:
                    self.logger.info(f"Skipping empty group '{subgroup_name}' - no layers match filter")
                    skip_group = True
            
            if skip_group:
                continue
                
            # Create subgroup
            new_group = parent_group.addGroup(subgroup_name)
            
            # Check if this has sub-groups
            if 'groups' in subgroup:
                # Process sub-groups recursively
                self.process_subgroups(subgroup.get('groups', []), new_group)
            
            # Check if this has layers and a service ID
            if 'layers' in subgroup and subgroup.get('source_service') and subgroup.get('source_service') != "":
                # Process layers
                self.process_layers(subgroup.get('layers', []), subgroup.get('source_service'), new_group)

    def process_layers(self, layers, service_id, parent_group):
        """
        Process and add layers to a group using batch processing.
        
        Args:
            layers: List of layers to process
            service_id: Service ID
            parent_group: Parent group to add layers to
        """
        # Get layer filter setting
        settings = QgsSettings()
        layer_filter = settings.value("ogc_layer_handler/layer_filter", "", type=str)
        filtered_layers = []
        
        if layer_filter:
            # Create a list of filtered layer IDs (strip whitespace)
            filter_ids = [lid.strip() for lid in layer_filter.split(',')]
            self.logger.info(f"Filtering layers. Only loading these IDs: {filter_ids}")
        else:
            filter_ids = None
            
        # First collect all layer information
        layer_tasks = []
        for layer_ref in layers:
            # Handle both dictionary and string formats for layer references
            if isinstance(layer_ref, dict):
                layer_id = layer_ref.get('id')
                # If the layer reference specifies a service, use that
                specified_service = layer_ref.get('service', None)
            else:
                layer_id = layer_ref
                specified_service = None
            
            # Skip this layer if we're filtering and it's not in the filter list
            if filter_ids and layer_id not in filter_ids:
                self.logger.debug(f"Skipping layer {layer_id} (not in filter list)")
                continue
                
            # Determine which service to use for this layer
            if specified_service:
                # Use the service specified in the layer reference
                use_service_id = specified_service
            elif isinstance(service_id, list) and len(service_id) == 1:
                # Only one service defined for this group
                use_service_id = service_id[0]
            elif isinstance(service_id, list) and len(service_id) > 1:
                # Multiple services, need to find which one has this layer
                use_service_id = self.find_service_for_layer(service_id, layer_id)
                if not use_service_id:
                    self.logger.error(f"Could not find service for layer {layer_id} in services {service_id}")
                    continue
            else:
                # Single service ID as string
                use_service_id = service_id
                
            self.logger.info(f"Adding layer task: {layer_id} from service: {use_service_id}")
            layer_tasks.append((use_service_id, layer_id))
        
        # Process layers in parallel
        max_workers = min(4, len(layer_tasks))  # Use at most 4 workers
        if max_workers > 0:
            # Create all layers in parallel
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                # Submit all tasks
                future_to_layer = {
                    executor.submit(self.create_layer, service_id, layer_id): (service_id, layer_id)
                    for service_id, layer_id in layer_tasks
                }
                
                # Create a list to hold layers to add to project
                layers_to_add = []
                
                # Process results as they complete
                for future in concurrent.futures.as_completed(future_to_layer):
                    service_id, layer_id = future_to_layer[future]
                    try:
                        layer = future.result()
                        if layer and layer.isValid():
                            # Store layer in the dictionary for later reference
                            if service_id not in self.layers_by_id:
                                self.layers_by_id[service_id] = {}
                            self.layers_by_id[service_id][layer_id] = layer
                            
                            self.logger.info(f"Layer {layer_id} is valid, adding to add list")
                            layers_to_add.append((layer, layer_id))
                        else:
                            self.logger.error(f"Layer {layer_id} creation failed or layer is invalid")
                    except Exception as e:
                        self.logger.error(f"Exception creating layer {layer_id}: {str(e)}")
                
                # Add all layers to project in batch
                for layer, layer_id in layers_to_add:
                    self.logger.info(f"Adding layer {layer_id} to project")
                    added_layer = QgsProject.instance().addMapLayer(layer, False)
                    parent_group.addLayer(added_layer)
        else:
            self.logger.info("No layer tasks to process")

    def find_service_for_layer(self, service_ids, layer_id):
        """
        Find which service contains a specific layer.
        
        Args:
            service_ids: List of service IDs to check
            layer_id: Layer ID to find
            
        Returns:
            str: Service ID containing the layer or None if not found
        """
        for service_id in service_ids:
            service_config = self.get_service_config(service_id)
            if not service_config:
                continue
                
            # For external services with 'layers'
            if not service_config.get('is_internal', False):
                for layer in service_config.get('layers', []):
                    if layer['id'] == layer_id:
                        return service_id
            # For internal TinyOWS services with 'collections'
            else:
                for collection in service_config.get('collections', []):
                    # Match against collection id
                    if collection.get('id') == layer_id:
                        return service_id
                    
        return None


class QGISPlugin:
    """
    QGIS Plugin for OGC Layer Handling with TinyOWS Support
    
    This plugin provides a QGIS interface for loading layers from external OGC services
    and internal TinyOWS services. It reads configuration from a JSON file and 
    builds a layer tree within QGIS. Now includes authentication support.
    """
    
    def __init__(self, iface):
        """
        Initialize the plugin.
        
        Args:
            iface: QGIS interface
        """
        self.iface = iface
        self.logger = setup_logging(level=logging.WARNING, tag='WS Grunddaten')
        self.layer_handler = None
        self.credential_manager = None
        self.style_manager = StyleManager(self.logger)
        self.title = 'WindScout Grunddaten Dienst'
        self.loaded_services = {}  # Initialize loaded_services dictionary to store layers
        
        # Initialize settings with defaults
        settings = QgsSettings()
        if not settings.contains("ogc_layer_handler/auto_load_styles"):
            settings.setValue("ogc_layer_handler/auto_load_styles", True)
        if not settings.contains("ogc_layer_handler/profiling_enabled"):
            settings.setValue("ogc_layer_handler/profiling_enabled", False)

    def profile_load_layers(self):
        """Profile the load_layers method and save results to a file."""
        # Check if profiling is enabled in settings
        settings = QgsSettings()
        profiling_enabled = settings.value("ogc_layer_handler/profiling_enabled", False, type=bool)
    
        if not profiling_enabled:
            # If profiling is disabled, just call load_layers directly
            self.load_layers()
            return
        try:
            # Create a profile object
            pr = cProfile.Profile()
            
            # Start profiling
            pr.enable()
            
            # Run the load_layers method
            self.load_layers()
            
            # Stop profiling
            pr.disable()
            
            # Create a StringIO object to capture the stats
            s = io.StringIO()
            
            # Sort stats by cumulative time
            ps = pstats.Stats(pr, stream=s).sort_stats('cumulative')
            
            # Print stats to StringIO object
            ps.print_stats()
            
            # Get the current timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            
            # Save to a file in the plugin directory
            profile_dir = os.path.join(os.path.dirname(__file__), 'profiles')
            os.makedirs(profile_dir, exist_ok=True)
            profile_file = os.path.join(profile_dir, f'load_layers_profile_{timestamp}.txt')
            
            with open(profile_file, 'w') as f:
                f.write(s.getvalue())
            
            self.logger.info(f"Profile saved to: {profile_file}")
            self.iface.messageBar().pushMessage(
                "Success", 
                f"Profile saved to: {profile_file}", 
                level=Qgis.Info
            )
            
        except Exception as e:
            self.logger.error(f"Error during profiling: {str(e)}")
            self.logger.error(traceback.format_exc())
            self.iface.messageBar().pushMessage(
                "Error", 
                f"Error during profiling: {str(e)}", 
                level=Qgis.Critical
            )

    def initGui(self):
        """Initialize the plugin GUI."""
        # Create actions
        self.load_action = QAction('Grundaten (neu) laden', self.iface.mainWindow())
        # self.refresh_action = QAction('Refresh Layers', self.iface.mainWindow())
        # self.check_tinyows_action = QAction('Check TinyOWS', self.iface.mainWindow())
        self.configure_action = QAction('Konfigurieren', self.iface.mainWindow())
        self.test_auth_action = QAction('Authentifizierung testen', self.iface.mainWindow())
        # self.test_direct_action = QAction('Test Direct HTTP Request', self.iface.mainWindow())
        
        # Add export/import style actions
        self.export_styles_action = QAction('Stile exportieren', self.iface.mainWindow())
        self.import_styles_action = QAction('Stile importieren', self.iface.mainWindow())
        self.load_server_styles_action = QAction('Stile vom Server laden', self.iface.mainWindow())
        
        # Add version display action
        self.version_action = QAction(f'Code Version: {PLUGIN_CODE_VERSION}', self.iface.mainWindow())
        
        # Connect actions to methods - Update load_action to use profiling
        self.load_action.triggered.connect(self.profile_load_layers)  # Changed from load_layers to profile_load_layers
        # Rest of the connections remain the same
        self.configure_action.triggered.connect(self.configure_server)
        self.test_auth_action.triggered.connect(self.test_auth_config)
        self.export_styles_action.triggered.connect(self.export_styles)
        self.import_styles_action.triggered.connect(self.import_styles)
        self.load_server_styles_action.triggered.connect(self.load_server_styles)
        self.version_action.triggered.connect(self.show_version)
        
        # Add actions to the Web menu
        self.iface.addPluginToWebMenu(f'&{self.title}', self.load_action)
        # self.iface.addPluginToWebMenu(f'&{self.title}', self.refresh_action)
        # self.iface.addPluginToWebMenu(f'&{self.title}', self.check_tinyows_action)
        self.iface.addPluginToWebMenu(f'&{self.title}', self.configure_action)
        self.iface.addPluginToWebMenu(f'&{self.title}', self.test_auth_action)
        # self.iface.addPluginToWebMenu(f'&{self.title}', self.test_direct_action)
        self.iface.addPluginToWebMenu(f'&{self.title}', self.export_styles_action)
        self.iface.addPluginToWebMenu(f'&{self.title}', self.import_styles_action)
        self.iface.addPluginToWebMenu(f'&{self.title}', self.load_server_styles_action)
        self.iface.addPluginToWebMenu(f'&{self.title}', self.version_action)  # Add version to menu
        
        # Optionally, add toolbar items
        self.toolbar = self.iface.addToolBar(f'{self.title}')
        self.toolbar.addAction(self.load_action)
        # self.toolbar.addAction(self.refresh_action)
        # self.toolbar.addAction(self.check_tinyows_action)
        # self.toolbar.addAction(self.configure_action)
        # self.toolbar.addAction(self.test_auth_action)
        # self.toolbar.addAction(self.test_direct_action)
        self.toolbar.addAction(self.export_styles_action)
        #self.toolbar.addAction(self.import_styles_action)
        #self.toolbar.addAction(self.load_server_styles_action)
        #self.toolbar.addAction(self.version_action)  # Add version to toolbar

        # Initialize settings with defaults
        settings = QgsSettings()
        if not settings.contains("ogc_layer_handler/auto_load_styles"):
            settings.setValue("ogc_layer_handler/auto_load_styles", True)
        if not settings.contains("ogc_layer_handler/profiling_enabled"):
            settings.setValue("ogc_layer_handler/profiling_enabled", False)
        
        # Check for config.json and create if it doesn't exist
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
        if not os.path.exists(config_path):
            self.logger.info(f"No config.json found at {config_path}, creating minimal config")
            
            # Get hostname and port from settings or use defaults
            hostname = settings.value("ogc_layer_handler/config_hostname", "localhost")
            port = settings.value("ogc_layer_handler/config_port", "443")
            
            # Create a minimal configuration
            minimal_config = {
                "hostname": hostname,
                "port": port,
                "services": {
                    "external_services": {},
                    "internal_services": {}
                },
                "styles": {},
                "layer_tree": []
            }
            
            try:
                # Ensure the directory exists
                os.makedirs(os.path.dirname(config_path), exist_ok=True)
                
                # Write the config file
                with open(config_path, 'w') as f:
                    json.dump(minimal_config, indent=2, fp=f)
                    
                self.logger.info(f"Created minimal config.json at {config_path}")
            except Exception as e:
                self.logger.error(f"Error creating config.json: {str(e)}")

        self.credential_manager = CredentialManager(self.logger)
        
        if not self.credential_manager.has_credentials():
            #Try to load pre-configured credentials
            if self.credential_manager.load_preconfigured_credentials():
                self.logger.info("Successfully loaded pre-configured credentials")
                self.iface.messageBar().pushMessage(
                    "Info", 
                    "Loaded pre-configured OGC credentials", 
                    level=Qgis.Info
                )
            else:
                self.logger.info("No pre-configured credentials found or failed to load them")
        else:
            auth_config_id = self.credential_manager.get_auth_config_id()
            self.logger.info(f"Credentials from auth cfg id: {auth_config_id}")
            # Get auth manager
            auth_manager = QgsApplication.authManager()
            
            # Get auth configuration
            auth_config = QgsAuthMethodConfig()
            if auth_manager.loadAuthenticationConfig(auth_config_id, auth_config, False):
                # this is a hack since loadAuthenticationConfig doesnt return false if authcfg doesnt exist
                if not auth_config.method():
                    self.logger.warning(f"Auth config {auth_config_id} not valid / not existing")
                    if self.credential_manager.load_preconfigured_credentials():
                        self.logger.info("Loaded pre-configured credentials")
                else:
                    self.logger.info(f"Loaded auth config: {auth_config_id}")

    def test_auth_config(self):
        """Test the authentication configuration by making a request to the server."""
        try:
            if not self.credential_manager:
                self.credential_manager = CredentialManager(self.logger)
            
            # Get authentication configuration ID
            auth_config_id = self.credential_manager.get_auth_config_id()
            if not auth_config_id:
                self.iface.messageBar().pushMessage(
                    "Error", 
                    "No authentication configuration found. Please configure API key first.", 
                    level=Qgis.Warning
                )
                return
            
            # Get hostname and port
            settings = QgsSettings()
            hostname = self.get_config_hostname()
            if not hostname:
                # If hostname is still not set, use a default value
                hostname = "localhost"
                settings.setValue("ogc_layer_handler/config_hostname", hostname)
                self.logger.warning(f"No hostname configured. Using default: {hostname}")
                self.iface.messageBar().pushMessage(
                    "Warning", 
                    f"No hostname configured. Using default: {hostname}", 
                    level=Qgis.Warning
                )
                
            port = self.get_config_port()
            protocol = 'https'
            if hostname in ['localhost', '127.0.0.1']:
                protocol = 'http'
            test_url = f"{protocol}://{hostname}:{port}/qgis_config"
            
            self.logger.info(f"Testing auth configuration with ID {auth_config_id} on URL: {test_url}")
            
            # Make request using QGIS network access manager with auth config
            from qgis.core import QgsNetworkAccessManager, QgsApplication, QgsAuthManager
            from qgis.PyQt.QtCore import QUrl
            from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply
            
            # Create request
            request = QNetworkRequest(QUrl(test_url))
            
            # Add basic headers
            request.setHeader(QNetworkRequest.UserAgentHeader, "QGIS OGC Layer Plugin Test")
            
            # Get auth manager and verify it's ready
            auth_manager = QgsApplication.authManager()
            if not auth_manager.isDisabled():
                self.logger.info("Auth manager is enabled")
            else:
                self.logger.warning("Auth manager is disabled, authentication might not work")
            
            # Load the auth config and log its details (without exposing the key)
            auth_config = QgsAuthMethodConfig()
            if auth_manager.loadAuthenticationConfig(auth_config_id, auth_config, True):
                method = auth_config.method()
                name = auth_config.name()
                self.logger.info(f"Loaded auth config: {name}, method: {method}")
                
                # Get the config map to verify it has the right structure
                config_map = auth_config.configMap()
                if not config_map:
                    self.logger.warning("Auth config is missing a value for the header")
                # header_name = config_map.get("header", "")
                self.logger.debug(f"Auth config is set to use headers: {config_map}")
                
                # Check if value exists (without logging it)
                
            else:
                self.logger.error(f"Failed to load auth config: {auth_manager.lastAuthenticationError()}")
            
            # Apply auth config to request - CRITICAL STEP
            auth_manager.updateNetworkRequest(request, auth_config_id)
            
            # Log request headers for debugging (without exposing sensitive data)
            self.logger.info("Request headers after auth applied:")
            headers = request.rawHeaderList()
            for header in headers:
                header_name = header.data().decode()
                # Don't log the actual API key
                if header_name != "X-API-KEY":
                    header_value = request.rawHeader(header).data().decode()
                    self.logger.info(f"  {header_name}: {header_value}")
                else:
                    self.logger.info(f"  {header_name}: [PRESENT]")
            
            # Get network access manager
            nam = QgsNetworkAccessManager.instance()
            
            # Make blocking request
            reply = nam.blockingGet(request)
            
            # Check response
            if reply.error() == 0:  # QNetworkReply.NoError
                content = reply.content().data().decode("utf-8", errors="replace")
                # Log a small snippet of the content (first 100 chars)
                content_preview = content[:100] + '...' if len(content) > 100 else content
                
                self.iface.messageBar().pushMessage(
                    "Success", 
                    f"Authentication test successful! Response size: {len(content)} bytes", 
                    level=Qgis.Success
                )
                self.logger.info(f"Authentication test successful. Response preview: {content_preview}")
            else:
                error_msg = reply.errorString()
                http_status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
                
                self.iface.messageBar().pushMessage(
                    "Error", 
                    f"Authentication test failed: {error_msg} (HTTP {http_status})", 
                    level=Qgis.Critical
                )
                self.logger.error(f"Authentication test failed: {error_msg}")
                self.logger.error(f"HTTP status code: {http_status}")
                
                # Log response content if available
                if reply.error() == QNetworkReply.NoError:
                    error_content = reply.content().data().decode("utf-8", errors="replace")
                    self.logger.error(f"Error response: {error_content}")
        except Exception as e:
            self.iface.messageBar().pushMessage(
                "Error", 
                f"Error testing authentication configuration: {str(e)}", 
                level=Qgis.Critical
            )
            self.logger.error(f"Error testing authentication configuration: {str(e)}")
            self.logger.error(traceback.format_exc())

    def configure_server(self):
        """Configure the server connection settings and API key authentication."""
        # Create a dialog
        dialog = QDialog(self.iface.mainWindow())
        dialog.setWindowTitle("OGC Server Configuration")
        dialog.resize(400, 350)  # Increased height for additional fields
        
        # Create tabs
        tabs = QTabWidget()
        server_tab = QWidget()
        auth_tab = QWidget()
        
        # Server tab
        server_layout = QVBoxLayout()
        server_form = QFormLayout()
        
        # Get existing settings
        hostname = self.get_config_hostname() or ""
        port = self.get_config_port()
        
        # Get auto-load styles setting
        settings = QgsSettings()
        auto_load_styles = settings.value("ogc_layer_handler/auto_load_styles", True, type=bool)
        
        # Get layer filter setting
        layer_filter = settings.value("ogc_layer_handler/layer_filter", "", type=str)
        
        # Create fields
        hostname_input = QLineEdit(hostname)
        port_input = QLineEdit(port)
        layer_filter_input = QLineEdit(layer_filter)
        layer_filter_input.setToolTip("Enter comma-separated layer IDs to only load specific layers")
        
        # Add fields to form
        server_form.addRow("Hostname:", hostname_input)
        server_form.addRow("Port:", port_input)
        server_form.addRow("Layer Filter (IDs):", layer_filter_input)
        
        # Create auto-load styles checkbox
        auto_styles_checkbox = QCheckBox("Automatically load styles from server when loading layers")
        auto_styles_checkbox.setChecked(auto_load_styles)
        auto_styles_checkbox.setToolTip("When enabled, styles will be automatically loaded from server after layers are loaded")
        
        # Add form to layout
        server_layout.addLayout(server_form)
        server_layout.addWidget(auto_styles_checkbox)
        
        # Set layout for server tab
        server_tab.setLayout(server_layout)
        
        # Authentication tab
        auth_layout = QVBoxLayout()
        auth_form = QFormLayout()
        
        # Create credential manager if not already created
        if not self.credential_manager:
            self.credential_manager = CredentialManager(self.logger)
        
        # Get existing credentials - now unpacking 4 values
        organization, api_key, save_key, auth_config_id = self.credential_manager.get_credentials()
        
        # Create fields
        organization_input = QLineEdit(organization)
        api_key_input = QLineEdit(api_key)
        
        # Add auth config ID info if available
        if auth_config_id:
            auth_config_label = QLabel(f"Authentication Configuration ID: {auth_config_id}")
            auth_layout.addWidget(auth_config_label)
        
        # Save password checkbox
        save_key_checkbox = QCheckBox("Save API key in QGIS Auth Database")
        save_key_checkbox.setChecked(save_key)
        
        # Add fields to form
        auth_form.addRow("Organization:", organization_input)
        auth_form.addRow("API Key:", api_key_input)
        
        # Add form and checkbox to layout
        auth_layout.addLayout(auth_form)
        auth_layout.addWidget(save_key_checkbox)
        
        # Set layout for auth tab
        auth_tab.setLayout(auth_layout)
        
        # Add tabs to tab widget
        tabs.addTab(server_tab, "Server")
        tabs.addTab(auth_tab, "Authentication")
        
        # Create buttons
        button_layout = QHBoxLayout()
        save_button = QPushButton("Save")
        cancel_button = QPushButton("Cancel")
        
        # Add buttons to layout
        button_layout.addWidget(save_button)
        button_layout.addWidget(cancel_button)
        
        # Create main layout
        main_layout = QVBoxLayout()
        main_layout.addWidget(tabs)
        main_layout.addLayout(button_layout)
        
        # Set layout for dialog
        dialog.setLayout(main_layout)
        
        # Create profiling checkbox
        profiling_enabled = settings.value("ogc_layer_handler/profiling_enabled", False, type=bool)
        profiling_checkbox = QCheckBox("Enable Performance Profiling")
        profiling_checkbox.setChecked(profiling_enabled)
        server_form.addRow("Profiling:", profiling_checkbox)
        
        # Connect buttons
        def save_settings():
            try:
                # Save server settings
                settings = QgsSettings()
                settings.setValue("ogc_layer_handler/config_hostname", hostname_input.text())
                settings.setValue("ogc_layer_handler/config_port", port_input.text())
                settings.setValue("ogc_layer_handler/auto_load_styles", auto_styles_checkbox.isChecked())
                settings.setValue("ogc_layer_handler/layer_filter", layer_filter_input.text())
                # Save profiling setting
                settings.setValue("ogc_layer_handler/profiling_enabled", profiling_checkbox.isChecked())
                # Save credentials
                organization = organization_input.text()
                api_key = api_key_input.text()
                save_key = save_key_checkbox.isChecked()
                
                self.credential_manager.save_credentials(organization, api_key, save_key)
                
                self.iface.messageBar().pushMessage("Success", "Server configuration saved", level=Qgis.Success)
                dialog.accept()
            except Exception as e:
                self.logger.error(f"Error saving settings: {str(e)}")
                self.iface.messageBar().pushMessage("Error", f"Failed to save settings: {str(e)}", level=Qgis.Critical)
        
        save_button.clicked.connect(save_settings)
        cancel_button.clicked.connect(dialog.reject)
        
        # Show the dialog
        dialog.exec_()

    def get_config_hostname(self):
        """Get hostname for configuration server.
        
        Returns:
            str: Hostname for the configuration server or None if not found
        """
        try:
            settings = QgsSettings()
            hostname = settings.value("ogc_layer_handler/config_hostname")
            
            # If no hostname is set, try to load from credentials file
            if not hostname:
                if not self.credential_manager:
                    self.credential_manager = CredentialManager(self.logger)
                self.credential_manager.load_preconfigured_credentials()
                # Try reading again after loading credentials
                hostname = settings.value("ogc_layer_handler/config_hostname")
                
            return hostname
        except Exception as e:
            self.logger.error(f"Error getting hostname: {str(e)}")
            return None
            
    def get_config_port(self):
        """Get port for configuration server.
        
        Returns:
            str: Port for the configuration server
        """
        try:
            settings = QgsSettings()
            port = settings.value("ogc_layer_handler/config_port")
            
            # If no port is set, use a default based on the hostname
            if not port:
                hostname = self.get_config_hostname()
                # Use standard port 443 for external hosts, 80 for localhost
                if hostname and hostname not in ['localhost', '127.0.0.1']:
                    port = "443"
                else:
                    port = "80"
                    
                # Save the default
                settings.setValue("ogc_layer_handler/config_port", port)
                self.logger.info(f"No port configured. Using default: {port}")
                
            return port
        except Exception as e:
            self.logger.error(f"Error getting port: {str(e)}")
            return "80"  # Default fallback
            
    def export_styles(self):
        """Export all layer styles to a JSON file"""
        try:
            # Create FileDialog to select destination
            file_dialog = QFileDialog()
            file_dialog.setAcceptMode(QFileDialog.AcceptSave)
            file_dialog.setNameFilter("JSON Files (*.json)")
            file_dialog.setDefaultSuffix("json")
            file_dialog.setWindowTitle("Export Layer Styles")
            
            # Set a default filename based on current date
            timestamp = datetime.now().strftime("%Y%m%d")
            file_dialog.selectFile(f"qgis_styles_{timestamp}.json")
            
            # Show the dialog to select destination path
            if file_dialog.exec_():
                # Get selected file path
                filepath = file_dialog.selectedFiles()[0]
                
                # Export styles
                success = self.style_manager.export_styles_to_file(self.loaded_services, filepath)
                
                if success:
                    self.iface.messageBar().pushMessage(
                        "Success",
                        f"Styles exported to {filepath}",
                        level=Qgis.Success
                    )
                else:
                    self.iface.messageBar().pushMessage(
                        "Error",
                        "Failed to export styles",
                        level=Qgis.Critical
                    )
        except Exception as e:
            self.logger.error(f"Error exporting styles: {str(e)}")
            self.logger.error(traceback.format_exc())
            self.iface.messageBar().pushMessage(
                "Error",
                f"Error exporting styles: {str(e)}",
                level=Qgis.Critical
            )
            
    def import_styles(self):
        """Import layer styles from a JSON file"""
        try:
            # Check if layers are loaded
            if not self.loaded_services:
                self.iface.messageBar().pushMessage(
                    "Warning",
                    "No layers loaded. Please load layers first.",
                    level=Qgis.Warning
                )
                return
                
            # Create FileDialog to select source file
            file_dialog = QFileDialog()
            file_dialog.setAcceptMode(QFileDialog.AcceptOpen)
            file_dialog.setNameFilter("JSON Files (*.json)")
            file_dialog.setWindowTitle("Import Layer Styles")
            
            # Show the dialog to select source file
            if file_dialog.exec_():
                # Get selected file path
                filepath = file_dialog.selectedFiles()[0]
                
                # Import styles with progress dialog
                self.iface.messageBar().pushMessage(
                    "Info",
                    "Importing styles... Please wait",
                    level=Qgis.Info
                )
                QgsApplication.processEvents()  # Keep UI responsive
                
                # Import styles
                success_count, fail_count = self.style_manager.import_styles_from_file(
                    self.loaded_services, filepath
                )
                
                self.iface.messageBar().pushMessage(
                    "Success",
                    f"Styles imported: {success_count} success, {fail_count} failed",
                    level=Qgis.Success if fail_count == 0 else Qgis.Warning
                )
        except Exception as e:
            self.logger.error(f"Error importing styles: {str(e)}")
            self.logger.error(traceback.format_exc())
            self.iface.messageBar().pushMessage(
                "Error",
                f"Error importing styles: {str(e)}",
                level=Qgis.Critical
            )
            
    def load_server_styles(self):
        """Load styles from server for all loaded layers"""
        try:
            # Check if layers are loaded
            if not self.loaded_services:
                self.iface.messageBar().pushMessage(
                    "Warning",
                    "No layers loaded. Please load layers first.",
                    level=Qgis.Warning
                )
                return
                
            # Get hostname and port
            hostname = self.get_config_hostname()
            if not hostname:
                self.iface.messageBar().pushMessage(
                    "Error",
                    "Hostname not configured. Please configure server settings first.",
                    level=Qgis.Critical
                )
                return
                
            port = self.get_config_port()
            
            # Get authentication header
            if not self.credential_manager:
                self.credential_manager = CredentialManager(self.logger)
                
            auth_header = self.credential_manager.get_auth_header()
            
            # Create progress dialog
            self.iface.messageBar().pushMessage(
                "Info",
                "Loading styles from server... Please wait",
                level=Qgis.Info
            )
            QgsApplication.processEvents()  # Keep UI responsive
            
            # Apply styles from server
            success_count, fail_count = self.style_manager.apply_server_styles(
                self.loaded_services, hostname, port, auth_header
            )
            
            if success_count > 0 or fail_count > 0:
                self.iface.messageBar().pushMessage(
                    "Success" if fail_count == 0 else "Warning",
                    f"Styles loaded: {success_count} success, {fail_count} failed",
                    level=Qgis.Success if fail_count == 0 else Qgis.Warning
                )
            else:
                self.iface.messageBar().pushMessage(
                    "Warning",
                    "No styles found on server or request failed",
                    level=Qgis.Warning
                )
        except Exception as e:
            self.logger.error(f"Error loading server styles: {str(e)}")
            self.logger.error(traceback.format_exc())
            self.iface.messageBar().pushMessage(
                "Error",
                f"Error loading server styles: {str(e)}",
                level=Qgis.Critical
            )
            
    def show_version(self):
        """Show plugin version information"""
        QMessageBox.information(
            self.iface.mainWindow(),
            "Plugin Version",
            f"WindScout Grunddaten Plugin\nCode Version: {PLUGIN_CODE_VERSION}\nGithub: https://github.com/GermanWindScout/renewable_basedata_plugin"
        )
        
    def load_layers(self):
        """Load layers from configuration using QGIS authentication system."""
        try:
            # Check if layer filtering is enabled
            settings = QgsSettings()
            layer_filter = settings.value("ogc_layer_handler/layer_filter", "", type=str)
            
            # Create credential manager if not already created
            if not self.credential_manager:
                self.credential_manager = CredentialManager(self.logger)
                
            # Get authentication configuration ID
            auth_config_id = self.credential_manager.get_auth_config_id()
            
            # Get hostname and port
            hostname = self.get_config_hostname()
            if not hostname:
                self.iface.messageBar().pushMessage(
                    "Error", 
                    "Server hostname not configured. Please configure server settings first.", 
                    level=Qgis.Critical
                )
                return
                
            port = self.get_config_port()
            if not port:
                port = "443"
            
            # Set connection parameters for initial load or refresh
            self.logger.info(f"Loading layers using hostname: {hostname}, port: {port}")
            
            # Determine config_path - either from a QgsSettings parameter or from the plugin directory
            config_path = settings.value("ogc_layer_handler/config_file_path", "")
            
            if not config_path:
                self.logger.warning("No config path was determined, using default config path.")
                # Use a default config path if none was determined
                config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
                
                # If a default config exists, make sure its hostname is updated
                if os.path.exists(config_path):
                    try:
                        with open(config_path, 'r') as f:
                            config_json = json.load(f)
                        
                        # Override hostname and port with values from QGIS settings
                        config_json['hostname'] = hostname
                        config_json['port'] = port
                        
                        with open(config_path, 'w') as f:
                            json.dump(config_json, f, indent=2)
                            
                        self.logger.info(f"Updated config.json with hostname: {hostname}, port: {port}")
                    except Exception as e:
                        self.logger.error(f"Error updating config.json: {str(e)}")
                # If a default config doesn't exist, create one
                else:
                    try:
                        # Create a default configuration
                        minimal_config = {
                            "hostname": hostname,
                            "port": port,
                            "services": {
                                "external_services": {},
                                "internal_services": {}
                            },
                            "styles": {},
                            "layer_tree": []
                        }
                        
                        with open(config_path, 'w') as f:
                            json.dump(minimal_config, f, indent=2)
                            
                        self.logger.info(f"Created default config.json at {config_path}")
                    except Exception as e:
                        self.logger.error(f"Error creating default config.json: {str(e)}")
                        
                # Check if we can download a configuration from the server
                try:
                    protocol = "https"
                    if hostname in ["localhost", "127.0.0.1"]:
                        protocol = "http"
                    
                    config_url = f"{protocol}://{hostname}:{port}/qgis_config"
                    self.logger.info(f"Attempting to download configuration from {config_url}")
                    
                    # Create a network request
                    request = QNetworkRequest(QUrl(config_url))
                    
                    # Add authentication if available
                    if auth_config_id:
                        auth_manager = QgsApplication.authManager()
                        auth_manager.updateNetworkRequest(request, auth_config_id)
                    
                    # Make blocking request
                    nam = QgsNetworkAccessManager.instance()
                    reply = nam.blockingGet(request)
                    
                    if reply.error() == QNetworkReply.NoError:
                        # Parse JSON response
                        response_text = bytes(reply.content()).decode('utf-8')
                        config_json = json.loads(response_text)
                        
                        # Override hostname and port with values from QGIS settings
                        config_json['hostname'] = hostname
                        config_json['port'] = port
                        
                        # Save to config file
                        with open(config_path, 'w') as f:
                            json.dump(config_json, f, indent=2)
                            
                        self.logger.info(f"Downloaded and saved configuration from {config_url}")
                    else:
                        self.logger.warning(f"Failed to download configuration: {reply.errorString()}")
                except Exception as e:
                    self.logger.error(f"Error downloading configuration: {str(e)}")
            
            # Create layer handler with config path and authentication
            self.layer_handler = LayerHandler(config_path, self.logger)
            self.layer_handler.credential_manager = self.credential_manager
            self.layer_handler.iface = self.iface
            
            # Override hostname and port in LayerHandler's config with values from QGIS settings
            self.layer_handler.config['hostname'] = hostname
            self.layer_handler.config['port'] = port
            
            # Set MetadataHandler's credential manager
            self.layer_handler.metadata_handler.set_credential_manager(self.credential_manager)
            
            # Build the layer tree
            self.logger.info("Building layer tree structure...")
            self.layer_handler.build_layer_tree()
            
            # Store reference to layers for style management
            self.loaded_services = self.layer_handler.layers_by_id
            
            # Optionally load styles from server
            if settings.value("ogc_layer_handler/auto_load_styles", True, type=bool):
                self.load_server_styles()
                
            self.iface.messageBar().pushMessage(
                "Success", 
                "Layers loaded successfully", 
                level=Qgis.Success
            )
            
        except Exception as e:
            self.logger.error(f"Error loading layers: {str(e)}")
            self.logger.error(traceback.format_exc())
            self.iface.messageBar().pushMessage(
                "Error", 
                f"Error loading layers: {str(e)}", 
                level=Qgis.Critical
            )

    def unload(self):
        """Removes the plugin menu items and icons from QGIS GUI."""
        # Remove the plugin menu items and icons
        self.iface.removePluginWebMenu(f'&{self.title}', self.load_action)
        # self.iface.removePluginWebMenu(f'&{self.title}', self.refresh_action)
        # self.iface.removePluginWebMenu(f'&{self.title}', self.check_tinyows_action)
        self.iface.removePluginWebMenu(f'&{self.title}', self.configure_action)
        self.iface.removePluginWebMenu(f'&{self.title}', self.test_auth_action)
        # self.iface.removePluginWebMenu(f'&{self.title}', self.test_direct_action)
        self.iface.removePluginWebMenu(f'&{self.title}', self.export_styles_action)
        self.iface.removePluginWebMenu(f'&{self.title}', self.import_styles_action)
        self.iface.removePluginWebMenu(f'&{self.title}', self.load_server_styles_action)
        self.iface.removePluginWebMenu(f'&{self.title}', self.version_action)
        
        # Remove the toolbar
        if hasattr(self, 'toolbar'):
            del self.toolbar
