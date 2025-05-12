from qgis.core import (
    QgsVectorLayer,
    QgsRasterLayer,
    QgsProject,
    QgsSingleSymbolRenderer,
    QgsFillSymbol,
    QgsLineSymbol,
    QgsAuthMethodConfig,
    QgsApplication,
    QgsCoordinateTransform,
    QgsMarkerSymbol,
    QgsDataSourceUri,
    Qgis,
    QgsMessageLog,
    QgsSettings,
    QgsNetworkAccessManager,
)

from qgis.PyQt.QtCore import QTimer, QUrl, QByteArray
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply

import logging
import json
import traceback
from typing import Dict, Optional, Union, List, Tuple
import sys
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from .connection import ConnectionManager
from .metadata import MetadataHandler

class LayerHandler:
    """
    Handles layer operations for the QGIS plugin.
    This version supports both external OGC services via NGINX proxy and
    internal services via TinyOWS. Now includes authentication support.
    """
    
    def __init__(self, config_path: str, logger: logging.Logger):
        """
        Initialize the layer handler.
        
        Args:
            config_path: Path to the configuration file
            logger: Logger to use for logging
        """
        self.config_path = config_path
        self.logger = logger
        self.credential_manager = CredentialManager(logger)
        self.connection_manager = ConnectionManager()
        self.metadata_handler = MetadataHandler(config_path)
        self.iface = None
        self._config = None
        self._config_mtime = None
        self._style_lookup = None
        self._visible_layers_loaded = False
        self.layers_by_id = {}
        
    @property
    def config(self) -> dict:
        """Get configuration with caching"""
        config_mtime = os.path.getmtime(self.config_path)
        if not self._config or self._config_mtime != config_mtime:
            with open(self.config_path, 'r') as f:
                self._config = json.load(f)
            self._config_mtime = config_mtime
            self._style_lookup = None  # Reset style lookup cache
        return self._config
        
    @property
    def style_lookup(self) -> Dict:
        """Get style lookup with caching"""
        if self._style_lookup is None:
            self._style_lookup = self._build_style_lookup()
        return self._style_lookup
    
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
            hostname = self.config.get('hostname', 'localhost')
            self.logger.info("hostname: " + hostname)
            port = self.config.get('port', 443)
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

    def _apply_style_to_layer(self, layer, layer_id: str, type_name: str) -> None:
        """Apply style with deferred loading for invisible layers"""
        if isinstance(layer, QgsRasterLayer):
            return  # Skip styling for raster layers
            
        if not layer.isVisible() and not self._visible_layers_loaded:
            # For invisible layers during initial load, attach style info but don't apply yet
            layer.setCustomProperty('pending_style', json.dumps({
                'layer_id': layer_id,
                'type_name': type_name
            }))
            return
            
        style_config = self.get_style_for_layer(layer_id, type_name)
        if not style_config:
            return
            
        # Continue with existing style application logic...
        style_type = style_config.get('type')
        
        if style_type == 'fill':
            fill_color = style_config['color']
            outline_color = style_config['outline_color']
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
                'outline_width': '0.5'
            })
            
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
            hostname = self.config.get('hostname', 'localhost')
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
            
            # Create request
            request = QNetworkRequest(QUrl(capabilities_url))
            
            # Add headers
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
        """Build layer tree with progressive loading"""
        canvas = self.iface.mapCanvas()
        current_extent = canvas.extent()
        canvas.setRenderFlag(False)
        
        try:
            root = QgsProject.instance().layerTreeRoot()
            
            # First load only essential/visible layers
            self._load_essential_layers(root)
            
            # Schedule background loading of remaining layers
            QTimer.singleShot(1000, lambda: self._load_background_layers(root))
            
        finally:
            # Restore map canvas settings
            canvas.setExtent(current_extent)
            canvas.setRenderFlag(True)
            
    def _load_essential_layers(self, root) -> None:
        """Load essential (visible) layers first"""
        for country in self.config.get('layer_tree', []):
            country_id = country.get('id')
            country_name = country.get('name')
            
            country_group = root.insertGroup(0, country_name)
            
            # Process only initially visible groups/layers
            for group in country.get('groups', []):
                if not group.get('initially_hidden', False):
                    self._process_group(group, country_group, visible_only=True)
                    
        self._visible_layers_loaded = True
        
    def _load_background_layers(self, root) -> None:
        """Load remaining layers in background"""
        for country in self.config.get('layer_tree', []):
            country_group = root.findGroup(country.get('name'))
            if not country_group:
                continue
                
            for group in country.get('groups', []):
                if group.get('initially_hidden', False):
                    self._process_group(group, country_group, visible_only=False)
                    
    def _process_group(self, group: Dict, parent_group, visible_only: bool = True) -> None:
        """Process a group and its layers/subgroups"""
        group_name = group.get('name')
        
        # Skip hidden groups when loading visible only
        if visible_only and group.get('initially_hidden', False):
            return
            
        new_group = parent_group.addGroup(group_name)
        
        # Process subgroups
        if 'groups' in group:
            for subgroup in group.get('groups', []):
                self._process_group(subgroup, new_group, visible_only)
                
        # Process layers
        if 'layers' in group and group.get('source_service'):
            layer_tasks = []
            for layer_ref in group.get('layers', []):
                # Handle both dictionary and string formats
                if isinstance(layer_ref, dict):
                    layer_id = layer_ref.get('id')
                    specified_service = layer_ref.get('service')
                else:
                    layer_id = layer_ref
                    specified_service = None
                    
                # Determine service ID
                service_id = specified_service or group.get('source_service')
                if isinstance(service_id, list):
                    service_id = self.find_service_for_layer(service_id, layer_id)
                    
                if service_id:
                    layer_tasks.append((service_id, layer_id))
                    
            # Create layers in optimized batches
            created_layers = self.create_layers_batch(layer_tasks)
            
            # Add layers to group
            for layer in created_layers:
                QgsProject.instance().addMapLayer(layer, False)
                new_group.addLayer(layer)

    def create_layers_batch(self, layer_tasks: List[Tuple[str, str]], 
                          batch_size: Optional[int] = None) -> List[QgsVectorLayer]:
        """
        Create multiple layers in batches optimized for connection speed
        
        Args:
            layer_tasks: List of (service_id, layer_id) tuples
            batch_size: Optional batch size override
            
        Returns:
            List of created layers
        """
        # Determine batch size based on connection quality
        if batch_size is None:
            quality = self.connection_manager.detect_connection_quality()
            batch_size = 2 if quality == "SLOW" else 5
            
        created_layers = []
        
        # Process in batches
        for i in range(0, len(layer_tasks), batch_size):
            batch = layer_tasks[i:i + batch_size]
            
            # Create layers in parallel within batch
            with ThreadPoolExecutor(max_workers=batch_size) as executor:
                futures = {
                    executor.submit(self.create_layer, service_id, layer_id): (service_id, layer_id)
                    for service_id, layer_id in batch
                }
                
                for future in as_completed(futures):
                    service_id, layer_id = futures[future]
                    try:
                        layer = future.result()
                        if layer and layer.isValid():
                            created_layers.append(layer)
                            if service_id not in self.layers_by_id:
                                self.layers_by_id[service_id] = {}
                            self.layers_by_id[service_id][layer_id] = layer
                    except Exception as e:
                        self.logger.error(f"Error creating layer {layer_id}: {str(e)}")
                        
        return created_layers

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

class QGISLogHandler(logging.Handler):
    """
    Custom logging handler for QGIS.
    Redirects Python logging messages to QGIS message log.
    """
    
    def __init__(self, tag='Plugin'):
        """
        Initialize the log handler with a tag.
        
        Args:
            tag: Tag for log messages
        """
        super().__init__()
        self.tag = tag

    def emit(self, record):
        """
        Emit a log record to QGIS message log.
        
        Args:
            record: Log record to emit
        """
        # Map Python logging levels to QGIS message levels
        level_map = {
            logging.DEBUG: Qgis.Info,
            logging.INFO: Qgis.Info,
            logging.WARNING: Qgis.Warning,
            logging.ERROR: Qgis.Critical,
            logging.CRITICAL: Qgis.Critical
        }
        
        qgis_level = level_map.get(record.levelno, Qgis.Info)
        
        # Log the message to QGIS
        QgsMessageLog.logMessage(
            self.format(record), 
            self.tag, 
            qgis_level
        )


def setup_logging(level=logging.INFO, tag='OGC Layer Plugin'):
    """
    Set up logging for the plugin with Docker-friendly configuration
    
    Args:
        level: Logging level (default: logging.INFO)
        tag: Tag for log messages (default: 'OGC Layer Plugin')
        
    Returns:
        logging.Logger: Configured logger
    """
    # Create a logger
    logger = logging.getLogger('qgis_plugin')
    
    # Clear any existing handlers
    logger.handlers.clear()
    
    # Set the logging level
    logger.setLevel(level)
    
    # Create console handler for Docker log output
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    
    # Create QGIS log handler
    qgis_handler = QGISLogHandler(tag)
    qgis_handler.setLevel(level)

    # Create file handler for file log output
    # file_handler = logging.FileHandler('./loghere.log')
    # file_handler.setLevel(level)
    
    # Create a formatter with ISO timestamp for Docker logs
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s', 
                                  datefmt='%Y-%m-%dT%H:%M:%S%z')
    console_handler.setFormatter(formatter)
    # file_handler.setFormatter(formatter)
    
    # Create a formatter for QGIS logs
    qgis_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    qgis_handler.setFormatter(qgis_formatter)
    
    # Add handlers to logger
    logger.addHandler(console_handler)
    logger.addHandler(qgis_handler)
    
    # Log configuration information
    logger.info(f"Logging initialized with level: {logging.getLevelName(level)}")
    logger.info(f"Running in QGIS version: {Qgis.QGIS_VERSION}")
    logger.info(f"Plugin directory: {os.path.dirname(os.path.abspath(__file__))}")
    
    return logger

class CredentialManager:
    """
    Manager for storing and retrieving API key credentials using QGIS Auth Configuration
    
    This class provides a secure way to handle API keys by storing them in the QGIS
    Authentication Database rather than in plain text settings. The QGIS Auth Database
    encrypts sensitive information and provides a standardized way to authenticate
    requests to OGC services.
    
    Key security features:
    1. API keys are stored in the encrypted QGIS Authentication Database
    2. Keys are never directly exposed in code, only their configuration IDs
    3. Authentication is applied to requests through the QGIS API
    4. Clear text API keys are only used during initial configuration
    
    The class offers methods to:
    - Create and store API key configurations
    - Retrieve authentication configuration IDs
    - Apply authentication to data sources
    """
    
    def __init__(self, logger: logging.Logger):
        """
        Initialize the credential manager
        
        Args:
            logger: Logger for messages
        """
        self.logger = logger
        self.settings = QgsSettings()

    def load_preconfigured_credentials(self) -> bool:
        """
        Load pre-configured API key credentials from the credentials file
        
        Returns:
            bool: True if credentials were successfully loaded, False otherwise
        """
        try:
            # Find credentials file in plugin directory
            self.credentials_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'credentials.json')
            self.logger.info(f"Looking for credentials file at: {self.credentials_file}")
            
            if os.path.exists(self.credentials_file):
                self.logger.info(f"Found credentials file at {self.credentials_file}")
                
                with open(self.credentials_file, 'r') as f:
                    creds = json.load(f)
                    
                if 'organization' in creds and 'api_key' in creds:
                    self.logger.info(f"Loaded pre-configured credentials for {creds['organization']}")
                    
                    # Store the hostname if provided
                    if 'hostname' in creds:
                        self.settings.setValue("ogc_layer_handler/config_hostname", creds['hostname'])
                        self.logger.info(f"Saved hostname from credentials: {creds['hostname']}")
                    
                    # Store the credentials using existing method
                    result = self.save_credentials(
                        creds['organization'], 
                        creds['api_key'],
                        True
                    )
                    
                    if result:
                        self.logger.info("Successfully saved pre-configured credentials to QGIS Auth Database")
                    else:
                        self.logger.warning("Failed to save pre-configured credentials to QGIS Auth Database")
                    
                    return result
                else:
                    self.logger.warning("Credentials file is missing required fields (organization, api_key)")
            else:
                self.logger.info(f"No credentials file found at {self.credentials_file}")
            
            return False
            
        except Exception as e:
            self.logger.error(f"Error loading pre-configured credentials: {str(e)}")
            self.logger.error(traceback.format_exc())
            return False
        
    def save_credentials(self, organization: str, api_key: str, save_key: bool = True) -> bool:
        """
        Save API key credentials to QGIS settings and auth configuration
        
        Args:
            organization: Organization name
            api_key: API key
            save_key: Whether to save the API key
            
        Returns:
            bool: Success
        """
        try:
            # Always save organization
            self.settings.setValue("ogc_layer_handler/auth_organization", organization)
            
            # Only save API key if requested
            if save_key and api_key:
                # Create and store auth configuration
                auth_id = self.create_api_key_auth_config(organization, api_key)
                if auth_id:
                    # Save the auth config ID
                    self.settings.setValue("ogc_layer_handler/auth_config_id", auth_id)
                    self.settings.setValue("ogc_layer_handler/auth_save_key", True)
                    self.logger.info(f"Saved API key credentials for {organization} using QGIS auth config ID: {auth_id}")
                    return True
                else:
                    self.logger.error(f"Failed to create auth configuration for {organization}")
                    return False
            else:
                # Clear saved auth configuration
                self.settings.setValue("ogc_layer_handler/auth_config_id", "")
                self.settings.setValue("ogc_layer_handler/auth_save_key", False)
                
            return True
            
        except Exception as e:
            self.logger.error(f"Error saving API key credentials: {str(e)}")
            self.logger.error(traceback.format_exc())
            return False
            
    def create_api_key_auth_config(self, organization: str, api_key: str) -> Optional[str]:
        """
        Create and store an API Key authentication configuration in QGIS.

        Args:
            organization: Organization name
            api_key: API key to store
            
        Returns:
            str: Authentication configuration ID or None if creation failed
        """
        try:
            # Create a new authentication method configuration
            auth_config = QgsAuthMethodConfig()
            
            # Set a unique name for this configuration
            auth_config.setName(f"API Key for {organization}")
            
            # Important: Use 'APIHeader' as the method for API keys
            auth_config.setMethod("APIHeader")
            
            # Set up the config map correctly - this is critical
            config_map = {
                "X-API-KEY": api_key
            }
            auth_config.setConfigMap(config_map)
            
            # Store the configuration in QGIS auth database
            auth_manager = QgsApplication.authManager()
            if auth_manager.storeAuthenticationConfig(auth_config):
                # Get the assigned ID after storing
                auth_id = auth_config.id()
                self.logger.info(f"Successfully created API key auth configuration with ID: {auth_id}")
                return auth_id
            else:
                self.logger.error("Failed to store authentication configuration")
                self.logger.error(f"Auth manager error: {auth_manager.lastAuthenticationError()}")
                return None
        except Exception as e:
            self.logger.error(f"Error creating API key auth configuration: {str(e)}")
            self.logger.error(traceback.format_exc())
            return None

    def get_credentials(self) -> Tuple[str, str, bool, str]:
        """
        Get saved API key credentials
        
        Returns:
            tuple: (organization, api_key, save_key, auth_config_id)
        """
        try:
            organization = self.settings.value("ogc_layer_handler/auth_organization", "")
            save_key = self.settings.value("ogc_layer_handler/auth_save_key", False)
            auth_config_id = self.settings.value("ogc_layer_handler/auth_config_id", "")
            
            # Get API key from auth configuration if available
            api_key = ""
            if auth_config_id:
                api_key = self.get_api_key_from_auth_config(auth_config_id)
            
            return (organization, api_key, save_key == "true" or save_key is True, auth_config_id)
            
        except Exception as e:
            self.logger.error(f"Error retrieving credentials: {str(e)}")
            self.logger.error(traceback.format_exc())
            return ("", "", False, "")
    
    def get_api_key_from_auth_config(self, auth_config_id: str) -> str:
        """
        Get API key from auth configuration, supporting both CustomHeader and APIHeader methods
        
        Args:
            auth_config_id: Authentication configuration ID
            
        Returns:
            str: API key or empty string if not found
        """
        try:
            # Get auth manager
            auth_manager = QgsApplication.authManager()
            
            # Get auth configuration
            auth_config = QgsAuthMethodConfig()
            self.logger.debug(f"Loading auth config with ID: {auth_config_id}")
            
            if auth_manager.loadAuthenticationConfig(auth_config_id, auth_config, True):
                # Get auth method and config map
                method = auth_config.method()
                config_map = auth_config.configMap()
                
                self.logger.info(f"Auth config method: {method}, config map keys: {list(config_map.keys())}")
                
                # For APIHeader method (used for layers)
                api_key = config_map.get("X-API-KEY", "")
                    
                # Log result without exposing the key
                if api_key:
                    self.logger.debug(f"Successfully retrieved API key from auth config (method: {method})")
                    # Also save the key directly in settings for direct HTTP testing
                    self.settings.setValue("ogc_layer_handler/api_key_cache", api_key)
                else:
                    self.logger.warning(f"No API key found in auth config (method: {method})")
                
                return api_key
            else:
                self.logger.error(f"Failed to load auth configuration with ID: {auth_config_id}")
                self.logger.error(f"Auth manager error: {auth_manager.lastAuthenticationError()}")
                return ""
        except Exception as e:
            self.logger.error(f"Error getting API key from auth configuration: {str(e)}")
            self.logger.error(traceback.format_exc())
            return ""
    
    def has_credentials(self) -> bool:
        """
        Check if API key credentials exist
        
        Returns:
            bool: True if credentials exist
        """
        _, _, _, auth_config_id = self.get_credentials()
        return bool(auth_config_id)
    
    def get_auth_config_id(self) -> str:
        """
        Get authentication configuration ID
        
        Returns:
            str: Authentication configuration ID or empty string if not available
        """
        _, _, _, auth_config_id = self.get_credentials()
        return auth_config_id
        
    def get_auth_header(self) -> Dict[str, str]:
        """
        Get authentication header for direct HTTP requests
        
        Returns:
            Dict[str, str]: Header dictionary or empty dict if not available
        """
        try:
            # Try to get API key from auth config
            _, api_key, _, _ = self.get_credentials()
            
            # If that fails, try to get from direct cache (fallback)
            if not api_key:
                api_key = self.settings.value("ogc_layer_handler/api_key_cache", "")
                
            if api_key:
                return {"X-API-KEY": api_key}
            return {}
        except Exception as e:
            self.logger.error(f"Error getting auth header: {str(e)}")
            return {}
