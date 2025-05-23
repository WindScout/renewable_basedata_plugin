"""
Domain models for WindScout Grunddaten Plugin

This module contains the core data models used throughout the plugin.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from datetime import datetime


@dataclass
class LayerMetadata:
    """Model representing layer metadata"""
    identifier: str = ""
    title: str = ""
    abstract: str = ""
    licenses: List[str] = field(default_factory=list)
    rights: List[str] = field(default_factory=list)
    extent: Dict = field(default_factory=dict)
    temporal_extent: Dict = field(default_factory=dict)
    custom_properties: Dict = field(default_factory=dict)
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'LayerMetadata':
        """Create a metadata instance from dictionary"""
        return cls(
            identifier=data.get('id', data.get('identifier', '')),
            title=data.get('title', ''),
            abstract=data.get('description', data.get('abstract', '')),
            licenses=[data.get('license')] if 'license' in data else data.get('licenses', []),
            rights=[data.get('attribution')] if 'attribution' in data else data.get('rights', []),
            extent=data.get('extent', {}),
            temporal_extent=data.get('temporal_extent', {}),
            custom_properties={
                'quality': data.get('quality', ''),
                'updated': data.get('updated', ''),
                'data_uri': data.get('data_uri', '')
            }
        )


@dataclass
class ServiceConfig:
    """Model representing service configuration"""
    id: str
    type: str = "WFS"
    proxy_path: str = ""
    region: str = ""
    is_internal: bool = False
    service_type: str = "WFS"
    metadata_mapping: Dict = field(default_factory=dict)
    
    @classmethod
    def from_dict(cls, data: Dict, region: str = "") -> 'ServiceConfig':
        """Create a service config instance from dictionary"""
        return cls(
            id=data.get('id', ''),
            type=data.get('type', 'WFS'),
            proxy_path=data.get('proxy_path', ''),
            region=region or data.get('region', ''),
            is_internal=data.get('is_internal', False),
            service_type=data.get('service_type', data.get('type', 'WFS')),
            metadata_mapping=data.get('metadata_mapping', {})
        )


@dataclass
class LayerConfig:
    """Model representing layer configuration"""
    id: str
    name: str = ""
    type_name: str = ""
    description: str = ""
    min_scale: Optional[float] = None
    min_zoom: int = 0
    max_zoom: int = 19
    format: str = "png"
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'LayerConfig':
        """Create a layer config instance from dictionary"""
        return cls(
            id=data.get('id', ''),
            name=data.get('name', data.get('id', '')),
            type_name=data.get('type_name', ''),
            description=data.get('description', ''),
            min_scale=float(data.get('min_scale')) if 'min_scale' in data else None,
            min_zoom=data.get('min_zoom', 0),
            max_zoom=data.get('max_zoom', 19),
            format=data.get('format', 'png')
        ) 