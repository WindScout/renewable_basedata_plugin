"""
Configuration management for the WindScout Grunddaten Plugin

This module provides a unified approach to configuration management,
combining QGIS settings, config files, and fallback values.
"""
import os
import json
import logging
from typing import Dict, Optional, Any

from qgis.core import QgsSettings


class ConfigManager:
    """
    Manages configuration from multiple sources with fallback strategies
    
    Order of precedence:
    1. QGIS Settings
    2. Config file values
    3. Default fallback values
    """
    
    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize the configuration manager.
        
        Args:
            config_path: Path to JSON configuration file (optional)
        """
        self.logger = logging.getLogger('qgis_plugin.config')
        self.settings = QgsSettings()
        self.config_path = config_path
        self._config = None
        self._config_mtime = None
        
    @property
    def config(self) -> Dict:
        """
        Get configuration with automatic reloading when file changes.
        
        Returns:
            Dict: Configuration dictionary
        """
        if not self.config_path or not os.path.exists(self.config_path):
            return {}
            
        try:
            current_mtime = os.path.getmtime(self.config_path)
            if self._config is None or self._config_mtime != current_mtime:
                self.logger.info(f"Loading configuration from {self.config_path}")
                with open(self.config_path, 'r') as f:
                    self._config = json.load(f)
                self._config_mtime = current_mtime
        except (FileNotFoundError, json.JSONDecodeError, Exception) as e:
            self.logger.error(f"Error loading config: {str(e)}")
            self._config = {}
            
        return self._config
    
    def get_hostname(self) -> str:
        """
        Get hostname with fallback strategy.
        
        Returns:
            str: Hostname for server connection
        """
        # First try QGIS settings
        hostname = self.settings.value("ogc_layer_handler/config_hostname")
        
        # If not found, try config file
        if not hostname:
            hostname = self.config.get('hostname')
            
        # Final fallback
        if not hostname:
            hostname = 'localhost'
            
        return hostname
        
    def get_port(self) -> str:
        """
        Get port with fallback strategy.
        
        Returns:
            str: Port for server connection
        """
        # First try QGIS settings
        port = self.settings.value("ogc_layer_handler/config_port")
        
        # If not found, try config file
        if not port:
            port = self.config.get('port')
            
        # Determine appropriate default based on hostname
        if not port:
            hostname = self.get_hostname()
            if hostname in ['localhost', '127.0.0.1']:
                port = '80'
            else:
                port = '443'
            
        return str(port)
    
    def get_service_config(self, service_id: str) -> Optional[Dict]:
        """
        Get configuration for a specific service.
        
        Args:
            service_id: Service ID to lookup
            
        Returns:
            Dict: Service configuration or None if not found
        """
        if not self.config or 'services' not in self.config:
            self.logger.warning(f"No services found in configuration")
            return None
            
        services = self.config.get('services', {})
        
        # Check external services across all federal states
        external_services = services.get('external_services', {})
        for state_code, state_services in external_services.items():
            for service in state_services:
                if service.get('id') == service_id:
                    # Add auto-generated proxy_path
                    service_config = service.copy()  # Create a copy to not modify original
                    # Handle XYZ tiles specifically
                    if service_config.get('type') == 'xyz_tiles':
                        service_config['proxy_path'] = f"/xyz/{state_code.lower()}/{service_id}"
                    else:
                        # Standard proxy path for WFS/WMS services
                        service_config['proxy_path'] = f"/ogc/{state_code.lower()}/{service_id}"
                    
                    # Store the region information
                    service_config['region'] = state_code
                    return service_config
        
        # Check internal services - for tinyows
        internal_services = services.get('internal_services', {})
        for state_code, state_services in internal_services.items():
            for service in state_services:
                if service.get('id') == service_id:
                    # Mark as internal tinyows service
                    service_config = service.copy()  # Create a copy to not modify original
                    service_config['is_internal'] = True
                    service_config['service_type'] = service_config.get('service_type', 'tinyows')
                    # Store the region information
                    service_config['region'] = state_code
                    return service_config
        
        self.logger.warning(f"Service ID {service_id} not found in configuration")
        return None
        
    def get_layer_config(self, service_id: str, layer_id: str) -> Optional[Dict]:
        """
        Get configuration for a specific layer.
        
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
                if layer.get('id') == layer_id:
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
                
        self.logger.warning(f"Layer ID {layer_id} not found in service {service_id}")
        return None
        
    def get_style_config(self, layer_id: str, type_name: str = "") -> Optional[Dict]:
        """
        Get style configuration for a layer.
        
        Args:
            layer_id: Layer ID
            type_name: Type name (optional)
            
        Returns:
            Dict: Style configuration or None if not found
        """
        styles = self.config.get('styles', {})
        
        # First try direct match by layer_id
        if layer_id in styles:
            return styles[layer_id]
            
        # If not found and type_name provided, try matching by type_name
        if type_name:
            for style_id, style_config in styles.items():
                applies_to = style_config.get('applies_to_type_names', [])
                if type_name in applies_to:
                    return style_config
                    
        return None
        
    def get_value(self, key: str, default: Any = None) -> Any:
        """
        Get a configuration value with fallback strategy.
        
        Args:
            key: Configuration key
            default: Default value if not found
            
        Returns:
            Any: Configuration value
        """
        # First try QGIS settings
        value = self.settings.value(f"ogc_layer_handler/{key}")
        
        # If not found, try config file
        if value is None:
            # Handle nested keys using dot notation (e.g. "services.external")
            if '.' in key:
                parts = key.split('.')
                cfg = self.config
                for part in parts:
                    if isinstance(cfg, dict) and part in cfg:
                        cfg = cfg[part]
                    else:
                        cfg = None
                        break
                value = cfg
            else:
                value = self.config.get(key)
            
        # Final fallback
        if value is None:
            value = default
            
        return value 