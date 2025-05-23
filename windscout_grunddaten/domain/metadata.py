"""
Core metadata business logic

This module contains the business logic for handling metadata.
"""
from typing import Dict, Optional
import logging
import json
from datetime import datetime

from qgis.core import QgsLayerMetadata, QgsBox3d, QgsDateTimeRange

from .models import LayerMetadata


class MetadataProcessor:
    """
    Core business logic for processing layer metadata
    
    This class handles the transformation between raw metadata dictionaries
    and QGIS metadata objects.
    """
    
    def __init__(self):
        """Initialize the metadata processor"""
        self.logger = logging.getLogger('qgis_plugin.metadata')
    
    def create_qgis_metadata(self, metadata: LayerMetadata) -> QgsLayerMetadata:
        """
        Create a QGIS layer metadata object from our domain model.
        
        Args:
            metadata: LayerMetadata instance
            
        Returns:
            QgsLayerMetadata: QGIS metadata object
        """
        qmd = QgsLayerMetadata()
        
        # Set basic metadata
        qmd.setIdentifier(metadata.identifier)
        qmd.setTitle(metadata.title)
        qmd.setAbstract(metadata.abstract)
        
        # Set spatial extent if available
        if metadata.extent:
            try:
                # Handle different extent formats
                if 'bbox' in metadata.extent:
                    bbox = metadata.extent['bbox']
                    qmd.setExtent(QgsBox3d(
                        bbox[0], bbox[1], 0,
                        bbox[2], bbox[3], 0
                    ))
                elif all(k in metadata.extent for k in ('xmin', 'ymin', 'xmax', 'ymax')):
                    qmd.setExtent(QgsBox3d(
                        metadata.extent['xmin'], metadata.extent['ymin'], 0,
                        metadata.extent['xmax'], metadata.extent['ymax'], 0
                    ))
            except (KeyError, IndexError) as e:
                self.logger.warning(f"Error setting spatial extent: {e}")
        
        # Set temporal extent if available
        if metadata.temporal_extent:
            try:
                if 'interval' in metadata.temporal_extent:
                    interval = metadata.temporal_extent['interval']
                    start = datetime.fromisoformat(interval[0])
                    end = datetime.fromisoformat(interval[1])
                    qmd.setTemporalExtents([QgsDateTimeRange(start, end)])
                elif all(k in metadata.temporal_extent for k in ('start', 'end')):
                    start = datetime.fromisoformat(metadata.temporal_extent['start'])
                    end = datetime.fromisoformat(metadata.temporal_extent['end'])
                    qmd.setTemporalExtents([QgsDateTimeRange(start, end)])
            except (ValueError, KeyError, IndexError) as e:
                self.logger.warning(f"Error setting temporal extent: {e}")
        
        # Set licenses and rights
        if metadata.licenses:
            qmd.setLicenses(metadata.licenses)
        
        if metadata.rights:
            qmd.setRights(metadata.rights)
        
        return qmd
    
    def apply_custom_properties(self, layer, metadata: LayerMetadata) -> None:
        """
        Apply custom properties to a QGIS layer.
        
        Args:
            layer: QGIS layer object
            metadata: LayerMetadata instance
        """
        if not layer or not hasattr(layer, 'setCustomProperty'):
            return
            
        for key, value in metadata.custom_properties.items():
            if value:  # Only set non-empty values
                layer.setCustomProperty(key, value)
                
    def prepare_metadata_deferred(self, layer, service_config=None, layer_config=None) -> None:
        """
        Store metadata information for deferred application.
        
        Args:
            layer: QGIS layer
            service_config: Service configuration dictionary
            layer_config: Layer configuration dictionary
        """
        if not layer or not hasattr(layer, 'setCustomProperty'):
            return
            
        # Store minimal information for later use
        layer.setCustomProperty('pending_metadata', json.dumps({
            'service_id': service_config.get('id') if service_config else '',
            'layer_id': layer_config.get('id') if layer_config else ''
        })) 