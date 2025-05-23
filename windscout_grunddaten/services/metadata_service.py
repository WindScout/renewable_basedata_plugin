"""
Metadata service for WindScout Grunddaten Plugin

This service handles all metadata operations and coordinates between
the domain logic and infrastructure components.
"""
import json
import logging
import os
import time
from typing import Dict, Optional

from qgis.core import QgsRasterLayer, QgsVectorLayer, QgsProject, QgsMapLayer

from ..domain.models import LayerMetadata, ServiceConfig, LayerConfig
from ..domain.metadata import MetadataProcessor
from ..infrastructure.config import ConfigManager
from ..infrastructure.network import MetadataClient


class MetadataService:
    """
    Service for handling layer metadata operations
    
    This service coordinates between the domain logic and infrastructure
    components to provide metadata functionality.
    """
    
    def __init__(self, config_manager: ConfigManager, metadata_client: MetadataClient):
        """
        Initialize the metadata service.
        
        Args:
            config_manager: Configuration manager
            metadata_client: Metadata client for fetching metadata
        """
        self.logger = logging.getLogger('qgis_plugin.metadata_service')
        self.config_manager = config_manager
        self.metadata_client = metadata_client
        self.metadata_processor = MetadataProcessor()
        
        # Setup cache
        cache_dir = os.path.join(os.path.expanduser('~'), '.qgis_metadata_cache')
        os.makedirs(cache_dir, exist_ok=True)
        self.cache_dir = cache_dir
        self.memory_cache = {}
        self.cache_times = {}
        
    def get_metadata(self, service_id: str, layer_id: str) -> Optional[LayerMetadata]:
        """
        Get metadata for a layer with caching.
        
        Args:
            service_id: Service ID
            layer_id: Layer ID
            
        Returns:
            LayerMetadata: Metadata or None if not found
        """
        # Check cache first
        cache_key = f"{service_id}_{layer_id}"
        metadata = self._get_from_cache(cache_key)
        if metadata:
            return metadata
            
        # Get service and layer config
        service_config = self.config_manager.get_service_config(service_id)
        if not service_config:
            self.logger.warning(f"No service configuration found for {service_id}")
            return None
            
        layer_config = self.config_manager.get_layer_config(service_id, layer_id)
        if not layer_config:
            self.logger.warning(f"No layer configuration found for {layer_id}")
            return None
            
        # Build metadata from configurations or fetch from service
        metadata = self._prepare_metadata(service_config, layer_config)
        if metadata:
            # Store in cache
            self._store_in_cache(cache_key, metadata)
            
        return metadata
    
    def apply_metadata_to_layer(self, layer, service_id: str = None, layer_id: str = None):
        """
        Apply metadata to a QGIS layer.
        
        Args:
            layer: QGIS layer
            service_id: Service ID
            layer_id: Layer ID
        """
        # Skip invalid layers
        if not layer or not layer.isValid():
            return
            
        # For layers that should have deferred metadata loading, store minimal info for later
        # Instead of checking visibility (which is a layer tree property, not layer property),
        # check if the layer is in the layer tree and whether it has the custom property 'defer_metadata'
        should_defer = False
        if isinstance(layer, QgsMapLayer) and layer.customProperty('defer_metadata'):
            should_defer = True
        else:
            # You could also check if the layer is not yet in the layer tree
            root = QgsProject.instance().layerTreeRoot()
            node = root.findLayer(layer.id()) if hasattr(layer, 'id') else None
            should_defer = not node
            
        if should_defer:
            if service_id and layer_id:
                self.metadata_processor.prepare_metadata_deferred(layer, 
                                                                 {'id': service_id},
                                                                 {'id': layer_id})
            return
            
        # Get metadata
        metadata = None
        if service_id and layer_id:
            metadata = self.get_metadata(service_id, layer_id)
            
        # If no metadata found, try reading from layer's custom property
        if not metadata and layer.customProperty('pending_metadata'):
            try:
                pending_data = json.loads(layer.customProperty('pending_metadata'))
                service_id = pending_data.get('service_id')
                layer_id = pending_data.get('layer_id')
                if service_id and layer_id:
                    metadata = self.get_metadata(service_id, layer_id)
            except:
                self.logger.warning(f"Failed to parse pending metadata for layer")
                
        # If still no metadata, use minimal info
        if not metadata:
            metadata = LayerMetadata(
                identifier=layer_id or layer.name(),
                title=layer.name(),
                abstract="No metadata available"
            )
            
        # Create QGIS metadata and apply to layer
        qgis_metadata = self.metadata_processor.create_qgis_metadata(metadata)
        layer.setMetadata(qgis_metadata)
        
        # Apply custom properties
        self.metadata_processor.apply_custom_properties(layer, metadata)
    
    def _prepare_metadata(self, service_config: Dict, layer_config: Dict) -> Optional[LayerMetadata]:
        """
        Prepare metadata from available sources.
        
        Args:
            service_config: Service configuration dictionary
            layer_config: Layer configuration dictionary
            
        Returns:
            LayerMetadata: Metadata or None if preparation failed
        """
        try:
            # Start with basic metadata from configs
            collection_id = layer_config.get('id')
            
            # First check if service has metadata_mapping
            if 'metadata_mapping' in service_config:
                self.logger.info(f"Using metadata mapping from service config for {collection_id}")
                metadata = {
                    'id': collection_id,
                    'title': layer_config.get('name', collection_id),
                    'description': layer_config.get('description', f"Layer {collection_id}")
                }
                
                # Add service-level metadata
                mapping = service_config['metadata_mapping']
                metadata.update({
                    'title': mapping.get('title', metadata['title']),
                    'description': mapping.get('description', metadata['description']),
                    'license': mapping.get('license'),
                    'attribution': mapping.get('author'),
                    'updated': mapping.get('updated'),
                    'data_uri': mapping.get('data_uri')
                })
                
                return LayerMetadata.from_dict(metadata)
            
            # If no metadata_mapping, try to fetch from service
            if service_config:
                raw_metadata = self.metadata_client.fetch_metadata(service_config, collection_id)
                if raw_metadata:
                    return LayerMetadata.from_dict(raw_metadata)
            
            # If all else fails, create minimal metadata
            return LayerMetadata(
                identifier=collection_id,
                title=layer_config.get('name', collection_id),
                abstract=layer_config.get('description', f"Layer {collection_id}")
            )
            
        except Exception as e:
            self.logger.error(f"Error preparing metadata: {str(e)}")
            return None
    
    def _get_from_cache(self, key: str) -> Optional[LayerMetadata]:
        """
        Get metadata from cache.
        
        Args:
            key: Cache key
            
        Returns:
            LayerMetadata: Metadata or None if not found
        """
        # Check memory cache first
        if key in self.memory_cache:
            if time.time() - self.cache_times[key] < 300:  # 5 minute TTL
                return self.memory_cache[key]
            else:
                del self.memory_cache[key]
                del self.cache_times[key]
                
        # Check disk cache
        cache_file = os.path.join(self.cache_dir, f"{key}.json")
        if os.path.exists(cache_file):
            if time.time() - os.path.getmtime(cache_file) < 3600:  # 1 hour TTL
                try:
                    with open(cache_file, 'r') as f:
                        data = json.load(f)
                    metadata = LayerMetadata.from_dict(data)
                    self.memory_cache[key] = metadata
                    self.cache_times[key] = time.time()
                    return metadata
                except Exception as e:
                    self.logger.warning(f"Failed to read metadata cache for {key}: {str(e)}")
                    
        return None
        
    def _store_in_cache(self, key: str, metadata: LayerMetadata) -> None:
        """
        Store metadata in cache.
        
        Args:
            key: Cache key
            metadata: LayerMetadata to store
        """
        # Store in memory cache
        self.memory_cache[key] = metadata
        self.cache_times[key] = time.time()
        
        # Prepare data for disk cache
        data = {
            'identifier': metadata.identifier,
            'title': metadata.title,
            'abstract': metadata.abstract,
            'licenses': metadata.licenses,
            'rights': metadata.rights,
            'extent': metadata.extent,
            'temporal_extent': metadata.temporal_extent,
            'custom_properties': metadata.custom_properties
        }
        
        # Store on disk
        cache_file = os.path.join(self.cache_dir, f"{key}.json")
        try:
            with open(cache_file, 'w') as f:
                json.dump(data, f)
        except Exception as e:
            self.logger.warning(f"Failed to write metadata cache for {key}: {str(e)}") 