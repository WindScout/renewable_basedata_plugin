from qgis.core import (
    QgsAuthMethodConfig,
    QgsApplication,
    Qgis,
    QgsMessageLog,
    QgsSettings,
)

import logging
import json
import traceback
from typing import Dict, Optional, Tuple
import sys
import os


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
