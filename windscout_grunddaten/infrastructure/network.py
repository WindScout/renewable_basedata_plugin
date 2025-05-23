"""
Network client for WindScout Grunddaten Plugin

This module provides a central place for making HTTP requests.
"""
import json
import logging
from typing import Dict, Optional, Any, Tuple

from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest
from qgis.core import QgsNetworkAccessManager, QgsNetworkReplyContent

from .auth import AuthManager
from .config import ConfigManager


class NetworkClient:
    """
    Client for making authenticated HTTP requests
    
    This class centralizes network request handling and authentication,
    reducing code duplication throughout the plugin.
    """
    
    def __init__(self, config_manager: ConfigManager, auth_manager: AuthManager):
        """
        Initialize the network client.
        
        Args:
            config_manager: Configuration manager
            auth_manager: Authentication manager
        """
        self.logger = logging.getLogger('qgis_plugin.network')
        self.config_manager = config_manager
        self.auth_manager = auth_manager
        self.network_manager = QgsNetworkAccessManager.instance()
        
    def get_base_url(self) -> str:
        """
        Get the base URL for API requests.
        
        Returns:
            str: Base URL
        """
        hostname = self.config_manager.get_hostname()
        port = self.config_manager.get_port()
        
        # Determine protocol based on hostname
        if hostname in ['localhost', '127.0.0.1']:
            protocol = 'http'
        else:
            protocol = 'https'
            port = '443'  # Use standard HTTPS port for non-localhost
            
        return f"{protocol}://{hostname}:{port}"
    
    def request(self, path: str, params: Dict = None) -> Tuple[bool, Dict, int]:
        """
        Make a GET request to the API.
        
        Args:
            path: API path
            params: Query parameters (optional)
            
        Returns:
            Tuple[bool, dict, int]: (success, response data, status code)
        """
        base_url = self.get_base_url()
        url = f"{base_url}{path}"
        
        # Add query parameters if provided
        if params:
            query_items = []
            for key, value in params.items():
                query_items.append(f"{key}={value}")
            url = f"{url}?{'&'.join(query_items)}"
            
        self.logger.debug(f"Making GET request to: {url}")
        
        # Create request
        request = QNetworkRequest(QUrl(url))
        
        # Apply authentication
        self.auth_manager.apply_auth_to_request(request)
        
        # Make blocking request
        reply = self.network_manager.blockingGet(request)
        
        return self._process_reply(reply)
    
    def _process_reply(self, reply: QgsNetworkReplyContent) -> Tuple[bool, Dict, int]:
        """
        Process a network reply.
        
        Args:
            reply: Network reply
            
        Returns:
            Tuple[bool, dict, int]: (success, response data, status code)
        """
        # Get status code
        status_code = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        
        # Check if request was successful
        if reply.error() == 0:  # QNetworkReply.NoError
            try:
                # Get response content
                content = bytes(reply.content()).decode('utf-8')
                
                # Try to parse as JSON
                try:
                    data = json.loads(content)
                    return True, data, status_code
                except json.JSONDecodeError:
                    # Return as plain text
                    return True, {"content": content}, status_code
                    
            except Exception as e:
                self.logger.error(f"Error processing response: {str(e)}")
                return False, {"error": str(e)}, status_code
        else:
            error_msg = reply.errorString()
            self.logger.error(f"Request failed: {error_msg}")
            self.logger.error(f"HTTP status code: {status_code}")
            
            # Try to get error response content
            try:
                error_content = bytes(reply.content()).decode('utf-8')
                self.logger.error(f"Error response: {error_content}")
                return False, {"error": error_msg, "content": error_content}, status_code
            except:
                return False, {"error": error_msg}, status_code


class MetadataClient:
    """
    Client for retrieving metadata from services
    
    This class handles the specific API calls for metadata retrieval.
    """
    
    def __init__(self, network_client: NetworkClient):
        """
        Initialize the metadata client.
        
        Args:
            network_client: Network client
        """
        self.logger = logging.getLogger('qgis_plugin.metadata_client')
        self.network_client = network_client
        
    def fetch_metadata(self, service_config: Dict, collection_id: str) -> Optional[Dict]:
        """
        Fetch metadata from a service.
        
        Args:
            service_config: Service configuration
            collection_id: Collection ID
            
        Returns:
            Dict: Metadata or None if request failed
        """
        # Different handling based on service type
        if service_config.get('is_internal', False):
            return self.fetch_tinyows_metadata(collection_id)
        else:
            return self.fetch_external_metadata(service_config, collection_id)
    
    def fetch_tinyows_metadata(self, collection_id: str) -> Optional[Dict]:
        """
        Fetch metadata for TinyOWS layers.
        
        Args:
            collection_id: Collection ID
            
        Returns:
            Dict: Metadata or None if request failed
        """
        try:
            # Capabilities URL for specific layer
            params = {
                "service": "WFS",
                "version": "1.1.0",
                "request": "DescribeFeatureType",
                "typename": collection_id
            }
            
            success, data, status_code = self.network_client.request("/tinyows", params)
            
            if success:
                # TinyOWS returns XML schema, but we can extract basic info
                return {
                    'id': collection_id,
                    'title': f"TinyOWS Layer: {collection_id}",
                    'description': f"WFS layer from TinyOWS service"
                }
            else:
                self.logger.warning(f"Failed to fetch metadata for collection {collection_id}")
                return None
                
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
            proxy_path = service_config.get('proxy_path', '')
            
            # Construct WFS GetCapabilities request through proxy
            params = {
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetCapabilities"
            }
            
            success, data, status_code = self.network_client.request(proxy_path, params)
            
            if success:
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
                        'updated': metadata_mapping.get('updated', None),
                        'data_uri': metadata_mapping.get('data_uri', None)
                    }
                # Default metadata if mapping not available
                return {
                    'id': collection_id,
                    'title': f"External Layer: {collection_id}",
                    'description': f"Metadata for layer {collection_id} from external WFS service"
                }
        except Exception as e:
           self.logger.error(f"Error fetching external metadata: {str(e)}")
        return None 