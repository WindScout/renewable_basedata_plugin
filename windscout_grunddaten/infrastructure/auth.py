"""
Authentication management for WindScout Grunddaten Plugin

This module handles authentication with API keys using QGIS Authentication System.
"""
import os
import json
import logging
import traceback
from typing import Dict, Optional, Tuple

from qgis.core import (
    QgsSettings,
    QgsAuthMethodConfig,
    QgsApplication
)
from qgis.PyQt.QtCore import QByteArray
from qgis.PyQt.QtNetwork import QNetworkRequest


class AuthManager:
    """
    Manager for authentication with API key credentials
    
    This class provides a secure way to handle API keys by storing them in the QGIS
    Authentication Database rather than in plain text settings.
    """
    
    def __init__(self, logger=None):
        """
        Initialize the authentication manager.
        
        Args:
            logger: Logger instance (optional)
        """
        self.logger = logger or logging.getLogger('qgis_plugin.auth')
        self.settings = QgsSettings()

    def load_preconfigured_credentials(self) -> bool:
        """
        Load pre-configured API key credentials from the credentials file
        
        Returns:
            bool: True if credentials were successfully loaded
        """
        try:
            # Find credentials file in plugin directory
            credentials_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'credentials.json')
            self.logger.info(f"Looking for credentials file at: {credentials_file}")
            
            if os.path.exists(credentials_file):
                self.logger.info(f"Found credentials file at {credentials_file}")
                
                with open(credentials_file, 'r') as f:
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
                    
                    return result
                else:
                    self.logger.warning("Credentials file is missing required fields (organization, api_key)")
            else:
                self.logger.info(f"No credentials file found at {credentials_file}")
            
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
            
            # Set the header and value
            auth_config.setConfig("X-API-KEY", api_key)
            
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
        Get API key from auth configuration
        
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
            
            if auth_manager.loadAuthenticationConfig(auth_config_id, auth_config, True):
                # Get the API key from the config
                value = auth_config.config("value")
                if value:
                    self.logger.debug(f"Successfully retrieved API key from auth config")
                    # Cache the key for direct HTTP testing
                    self.settings.setValue("ogc_layer_handler/api_key_cache", value)
                    return value
                else:
                    self.logger.warning(f"No API key found in auth config")
                    return ""
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
            str: Authentication configuration ID or empty string
        """
        _, _, _, auth_config_id = self.get_credentials()
        return auth_config_id
        
    def get_auth_header(self) -> Dict[str, str]:
        """
        Get authentication header for direct HTTP requests
        
        Returns:
            Dict[str, str]: Header dictionary
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
            
    def apply_auth_to_request(self, request: QNetworkRequest) -> None:
        """
        Apply authentication to a network request
        
        Args:
            request: QNetworkRequest object
        """
        try:
            # Get authentication header
            auth_header = self.get_auth_header()
            
            # Apply headers
            for header_name, header_value in auth_header.items():
                request.setRawHeader(
                    QByteArray(header_name.encode()),
                    QByteArray(str(header_value).encode())
                )
                
            # Get authentication configuration ID
            auth_config_id = self.get_auth_config_id()
            if auth_config_id:
                # Apply authentication configuration
                auth_manager = QgsApplication.authManager()
                auth_manager.updateNetworkRequest(request, auth_config_id)
                self.logger.debug(f"Applied auth config ID {auth_config_id} to request")
                
        except Exception as e:
            self.logger.error(f"Error applying authentication to request: {str(e)}")
            self.logger.error(traceback.format_exc()) 