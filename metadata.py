from qgis.core import QgsLayerMetadata, QgsBox3d, QgsDateTimeRange
import os
import json
import logging
from typing import Dict, Optional
import time

class MetadataCache:
    """Cache for layer metadata to avoid repeated loading"""
    
    def __init__(self, cache_dir: str = None):
        self.cache_dir = cache_dir or os.path.join(os.path.expanduser('~'), '.qgis_metadata_cache')
        os.makedirs(self.cache_dir, exist_ok=True)
        self.memory_cache = {}
        self.cache_times = {}
        self.logger = logging.getLogger(__name__)
        
    def get(self, key: str) -> Optional[Dict]:
        """Get metadata from cache"""
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
                        metadata = json.load(f)
                    self.memory_cache[key] = metadata
                    self.cache_times[key] = time.time()
                    return metadata
                except:
                    self.logger.warning(f"Failed to read metadata cache for {key}")
                    
        return None
        
    def set(self, key: str, metadata: Dict):
        """Store metadata in cache"""
        # Store in memory
        self.memory_cache[key] = metadata
        self.cache_times[key] = time.time()
        
        # Store on disk
        cache_file = os.path.join(self.cache_dir, f"{key}.json")
        try:
            with open(cache_file, 'w') as f:
                json.dump(metadata, f)
        except:
            self.logger.warning(f"Failed to write metadata cache for {key}")

class MetadataHandler:
    """Handles layer metadata with caching and optimized loading"""
    
    def __init__(self, config_path: str = None):
        self.config_path = config_path
        self._config = None
        self._config_mtime = None
        self.cache = MetadataCache()
        self.logger = logging.getLogger(__name__)
        self.credential_manager = None
        
    @property
    def config(self) -> dict:
        """Get configuration with caching"""
        if not self.config_path:
            return {}
            
        config_mtime = os.path.getmtime(self.config_path)
        if not self._config or self._config_mtime != config_mtime:
            with open(self.config_path, 'r') as f:
                self._config = json.load(f)
            self._config_mtime = config_mtime
        return self._config
        
    def set_credential_manager(self, credential_manager):
        """Set credential manager for authenticated requests"""
        self.credential_manager = credential_manager
        
    def get_layer_metadata(self, service_id: str, layer_id: str) -> Optional[Dict]:
        """Get metadata for layer with caching"""
        cache_key = f"{service_id}_{layer_id}"
        
        # Check cache first
        metadata = self.cache.get(cache_key)
        if metadata:
            return metadata
            
        # Get from config or fetch from service
        metadata = self.get_layer_metadata_from_config(service_id, layer_id)
        if metadata:
            self.cache.set(cache_key, metadata)
            
        return metadata
        
    def apply_metadata_to_layer(self, layer, service_config=None, layer_config=None):
        """Apply metadata to layer with optimizations"""
        if not layer or not layer.isValid():
            return
            
        # For invisible layers during initial load, just store metadata info
        if not layer.isVisible():
            layer.setCustomProperty('pending_metadata', json.dumps({
                'service_config': service_config,
                'layer_config': layer_config
            }))
            return
            
        metadata = self._prepare_metadata(service_config, layer_config)
        if not metadata:
            return
            
        qmd = QgsLayerMetadata()
        
        # Set basic metadata
        qmd.setIdentifier(metadata.get('identifier', ''))
        qmd.setTitle(metadata.get('title', ''))
        qmd.setAbstract(metadata.get('abstract', ''))
        
        # Set spatial extent if available
        if 'extent' in metadata:
            extent = metadata['extent']
            qmd.setExtent(QgsBox3d(
                extent['xmin'], extent['ymin'], 0,
                extent['xmax'], extent['ymax'], 0
            ))
            
        # Set temporal extent if available
        if 'temporal_extent' in metadata:
            temp = metadata['temporal_extent']
            qmd.setTemporalExtents([
                QgsDateTimeRange(temp['start'], temp['end'])
            ])
            
        # Set licenses
        if 'licenses' in metadata:
            qmd.setLicenses(metadata['licenses'])
            
        # Set rights
        if 'rights' in metadata:
            qmd.setRights(metadata['rights'])
            
        # Set custom properties
        if 'custom_properties' in metadata:
            for key, value in metadata['custom_properties'].items():
                layer.setCustomProperty(key, value)
                
        # Apply metadata to layer
        layer.setMetadata(qmd)
        
    def _prepare_metadata(self, service_config=None, layer_config=None) -> Optional[Dict]:
        """Prepare metadata dictionary from configs"""
        if not service_config and not layer_config:
            return None
            
        metadata = {}
        
        # Combine service and layer metadata
        if service_config:
            metadata.update({
                'identifier': service_config.get('id', ''),
                'title': service_config.get('title', ''),
                'abstract': service_config.get('description', ''),
                'licenses': service_config.get('licenses', []),
                'rights': service_config.get('rights', [])
            })
            
        if layer_config:
            metadata.update({
                'identifier': layer_config.get('id', metadata.get('identifier', '')),
                'title': layer_config.get('title', metadata.get('title', '')),
                'abstract': layer_config.get('description', metadata.get('abstract', '')),
                'extent': layer_config.get('extent', {}),
                'temporal_extent': layer_config.get('temporal_extent', {}),
                'custom_properties': layer_config.get('custom_properties', {})
            })
            
        return metadata 