"""
Layer service for WindScout Grunddaten Plugin

This service handles layer operations, including creation, loading,
and management of layer trees.
"""
import logging
import concurrent.futures
from typing import Dict, List, Optional, Tuple, Union

from qgis.core import (
    QgsVectorLayer,
    QgsRasterLayer,
    QgsProject,
    QgsCoordinateTransform,
    QgsDataSourceUri,
    QgsLayerTreeGroup,
    QgsMapLayer,
)
from qgis.PyQt.QtCore import QTimer

from ..infrastructure.config import ConfigManager
from ..infrastructure.auth import AuthManager
from .metadata_service import MetadataService
from .style_service import StyleService


class LayerService:
    """
    Service for handling layer operations
    
    This service coordinates layer creation, loading, and management
    using other services for specific tasks.
    """
    
    def __init__(
        self, 
        config_manager: ConfigManager,
        auth_manager: AuthManager,
        metadata_service: MetadataService,
        style_service: StyleService
    ):
        """
        Initialize the layer service.
        
        Args:
            config_manager: Configuration manager
            auth_manager: Authentication manager
            metadata_service: Metadata service
            style_service: Style service
        """
        self.logger = logging.getLogger('qgis_plugin.layer_service')
        self.config_manager = config_manager
        self.auth_manager = auth_manager
        self.metadata_service = metadata_service
        self.style_service = style_service
        self.iface = None
        self.visible_layers_loaded = False
        self.layers_by_id = {}
        
    def set_iface(self, iface) -> None:
        """
        Set the QGIS interface.
        
        Args:
            iface: QGIS interface
        """
        self.iface = iface
        
    def create_layer(self, service_id: str, layer_id: str) -> Optional[QgsMapLayer]:
        """
        Create a QGIS layer from service and layer IDs.
        
        Args:
            service_id: Service ID
            layer_id: Layer ID
            
        Returns:
            QgsMapLayer: Created layer or None if creation failed
        """
        try:
            self.logger.info(f"Creating layer with service_id: {service_id}, layer_id: {layer_id}")
            
            # Get configurations
            service_config = self.config_manager.get_service_config(service_id)
            if not service_config:
                self.logger.warning(f"No service configuration found for {service_id}")
                return None
                
            layer_config = self.config_manager.get_layer_config(service_id, layer_id)
            if not layer_config:
                self.logger.warning(f"No layer configuration found for {layer_id}")
                return None
                
            # Get common settings
            hostname = self.config_manager.get_hostname()
            port = self.config_manager.get_port()
            
            if hostname in ['127.0.0.1', 'localhost']:
                protocol = 'http'
            else:
                protocol = 'https'
                port = '443'
                
            # Get current map canvas settings
            if not self.iface:
                self.logger.error("QGIS interface not set")
                return None
                
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
            auth_config_id = self.auth_manager.get_auth_config_id()
            if auth_config_id:
                self.logger.info(f"Using authentication configuration ID: {auth_config_id}")
            else:
                self.logger.warning("No authentication configuration found")
                
            # Common layer settings
            layer_name = layer_config.get('name', layer_id)
            
            # Determine layer type and create appropriate layer
            is_internal = service_config.get('is_internal', False)
            service_type = service_config.get('type', 'WFS')
            
            if service_type == 'xyz_tiles':
                layer = self._create_xyz_layer(
                    hostname, port, service_config, layer_config, layer_name, auth_config_id
                )
            elif service_type == 'WMS' and not is_internal:
                layer = self._create_wms_layer(
                    hostname, port, service_config, layer_config, layer_name, 
                    auth_config_id, target_crs
                )
            else:
                layer = self._create_wfs_layer(
                    hostname, port, service_config, layer_config, layer_name,
                    auth_config_id, target_crs
                )
                
            if not layer or not layer.isValid():
                self.logger.critical(f"Layer {layer_name} is not valid")
                return None
                
            self.logger.info(f"Layer created successfully: {layer_name}")
            
            # Apply scale-based visibility if configured
            if isinstance(layer_config, dict) and layer_config.get('min_scale'):
                min_scale = float(layer_config['min_scale'])
                self.logger.info(f"Setting minimum scale for {layer_name} to 1:{min_scale}")
                layer.setMaximumScale(0)  # No maximum (fully zoomed in)
                layer.setMinimumScale(min_scale)  # Will hide when zoomed out beyond this
                layer.setScaleBasedVisibility(True)
                
            # Apply style
            self.style_service.apply_style(layer, layer_id, 
                                           layer_config.get('type_name', '') if isinstance(layer_config, dict) else '')
            
            # Apply metadata
            self.metadata_service.apply_metadata_to_layer(layer, service_id, layer_id)
            
            # Store in layers by ID dictionary
            if service_id not in self.layers_by_id:
                self.layers_by_id[service_id] = {}
            self.layers_by_id[service_id][layer_id] = layer
            
            return layer
            
        except Exception as e:
            self.logger.critical(f"Error creating layer: {str(e)}")
            import traceback
            self.logger.critical(traceback.format_exc())
            return None
            
    def _create_xyz_layer(self, hostname: str, port: str, 
                         service_config: Dict, layer_config: Dict, 
                         layer_name: str, auth_config_id: str) -> QgsRasterLayer:
        """
        Create an XYZ tiles layer.
        
        Args:
            hostname: Server hostname
            port: Server port
            service_config: Service configuration
            layer_config: Layer configuration
            layer_name: Layer name
            auth_config_id: Authentication configuration ID
            
        Returns:
            QgsRasterLayer: Created layer
        """
        protocol = 'https' if hostname not in ['127.0.0.1', 'localhost'] else 'http'
        proxy_path = service_config.get('proxy_path', '')
        
        # Construct the complete URL with placeholders and format extension
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
        return QgsRasterLayer(uri_str, layer_name, "wms")
        
    def _create_wms_layer(self, hostname: str, port: str,
                         service_config: Dict, layer_config: Dict,
                         layer_name: str, auth_config_id: str,
                         target_crs) -> QgsRasterLayer:
        """
        Create a WMS layer.
        
        Args:
            hostname: Server hostname
            port: Server port
            service_config: Service configuration
            layer_config: Layer configuration
            layer_name: Layer name
            auth_config_id: Authentication configuration ID
            target_crs: Target CRS
            
        Returns:
            QgsRasterLayer: Created layer
        """
        protocol = 'https' if hostname not in ['127.0.0.1', 'localhost'] else 'http'
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
        return QgsRasterLayer(wms_uri_string, layer_name, "wms")
        
    def _create_wfs_layer(self, hostname: str, port: str,
                         service_config: Dict, layer_config: Dict,
                         layer_name: str, auth_config_id: str,
                         target_crs) -> QgsVectorLayer:
        """
        Create a WFS layer.
        
        Args:
            hostname: Server hostname
            port: Server port
            service_config: Service configuration
            layer_config: Layer configuration
            layer_name: Layer name
            auth_config_id: Authentication configuration ID
            target_crs: Target CRS
            
        Returns:
            QgsVectorLayer: Created layer
        """
        protocol = 'https' if hostname not in ['127.0.0.1', 'localhost'] else 'http'
        is_internal = service_config.get('is_internal', False)
        region = service_config.get('region', '').lower()
        
        # For TinyOWS, construct type name with namespace (region:collection_id)
        if is_internal:
            type_name = f"{region}:{layer_config.get('id')}"
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
        return QgsVectorLayer(wfs_uri.uri(False), layer_name, "WFS")
        
    def build_layer_tree(self) -> None:
        """
        Build the layer tree structure with progressive loading.
        
        This loads visible layers first, then loads remaining layers in the background.
        """
        self.logger.info("Starting layer tree build...")
        
        if not self.iface:
            self.logger.error("QGIS interface not set")
            return
            
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
            self.logger.info("Initial layer tree build completed")
            
    def _load_essential_layers(self, root) -> None:
        """
        Load essential (visible) layers first.
        
        Args:
            root: Layer tree root
        """
        # Get layer filter
        layer_filter = self.config_manager.get_value("layer_filter", "")
        filter_ids = [lid.strip() for lid in layer_filter.split(',')] if layer_filter else None
        
        for country in self.config_manager.config.get('layer_tree', []):
            country_name = country.get('name')
            
            country_group = root.insertGroup(0, country_name)
            
            # Process only initially visible groups/layers
            for group in country.get('groups', []):
                if not group.get('initially_hidden', False):
                    self._process_group(group, country_group, visible_only=True, 
                                      filter_ids=filter_ids)
                    
        self.visible_layers_loaded = True
        
    def _load_background_layers(self, root) -> None:
        """
        Load remaining layers in background.
        
        Args:
            root: Layer tree root
        """
        # Get layer filter
        layer_filter = self.config_manager.get_value("layer_filter", "")
        filter_ids = [lid.strip() for lid in layer_filter.split(',')] if layer_filter else None
        
        for country in self.config_manager.config.get('layer_tree', []):
            country_group = root.findGroup(country.get('name'))
            if not country_group:
                continue
                
            for group in country.get('groups', []):
                if group.get('initially_hidden', False):
                    self._process_group(group, country_group, visible_only=False,
                                      filter_ids=filter_ids)
        
        # Apply deferred styles
        layers = []
        for service_layers in self.layers_by_id.values():
            layers.extend(service_layers.values())
            
        self.style_service.apply_deferred_styles(layers)
                    
    def _process_group(self, group: Dict, parent_group, visible_only: bool = True,
                     filter_ids: List[str] = None) -> None:
        """
        Process a group and its layers/subgroups.
        
        Args:
            group: Group configuration
            parent_group: Parent group
            visible_only: Only process visible layers
            filter_ids: List of layer IDs to filter
        """
        group_name = group.get('name')
        
        # Skip hidden groups when loading visible only
        if visible_only and group.get('initially_hidden', False):
            return
            
        new_group = parent_group.addGroup(group_name)
        
        # Process subgroups
        if 'groups' in group:
            for subgroup in group.get('groups', []):
                self._process_group(subgroup, new_group, visible_only, filter_ids)
                
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
                    
                # Skip if filtering and not in filter list
                if filter_ids and layer_id not in filter_ids:
                    self.logger.debug(f"Skipping layer {layer_id} (not in filter list)")
                    continue
                    
                # Determine service ID
                service_id = specified_service or group.get('source_service')
                if isinstance(service_id, list):
                    service_id = self._find_service_for_layer(service_id, layer_id)
                    
                if service_id:
                    layer_tasks.append((service_id, layer_id))
            
            # Process layers in batch
            created_layers = self.create_layers_batch(layer_tasks)
            
            # Add layers to group
            for layer in created_layers:
                QgsProject.instance().addMapLayer(layer, False)
                new_group.addLayer(layer)
                
    def create_layers_batch(self, layer_tasks: List[Tuple[str, str]], 
                          batch_size: int = 4) -> List[QgsMapLayer]:
        """
        Create multiple layers in batches.
        
        Args:
            layer_tasks: List of (service_id, layer_id) tuples
            batch_size: Number of layers to create in parallel
            
        Returns:
            List[QgsMapLayer]: Created layers
        """
        created_layers = []
        
        # Process in batches
        for i in range(0, len(layer_tasks), batch_size):
            batch = layer_tasks[i:i + batch_size]
            
            # Create layers in parallel within batch
            with concurrent.futures.ThreadPoolExecutor(max_workers=batch_size) as executor:
                futures = {
                    executor.submit(self.create_layer, service_id, layer_id): (service_id, layer_id)
                    for service_id, layer_id in batch
                }
                
                for future in concurrent.futures.as_completed(futures):
                    service_id, layer_id = futures[future]
                    try:
                        layer = future.result()
                        if layer and layer.isValid():
                            created_layers.append(layer)
                    except Exception as e:
                        self.logger.error(f"Error creating layer {layer_id}: {str(e)}")
                        
        return created_layers
        
    def _find_service_for_layer(self, service_ids: List[str], layer_id: str) -> Optional[str]:
        """
        Find which service contains a specific layer.
        
        Args:
            service_ids: List of service IDs to check
            layer_id: Layer ID to find
            
        Returns:
            str: Service ID containing the layer or None if not found
        """
        for service_id in service_ids:
            service_config = self.config_manager.get_service_config(service_id)
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