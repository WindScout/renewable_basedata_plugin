"""
QGIS Plugin providing WindScout basic datasets for renewables project development

This plugin loads data from Geoserver instances via NGINX Proxy, including both external OGC services 
internal TinyOWS services, and XYZ tile services. It reads configuration from a JSON file and
provides robust error handling and authentication support.

"""

from __future__ import annotations
import os
import logging
import traceback
import json
import base64
import re
from datetime import datetime
from typing import Dict, Optional, Tuple, Union

from qgis.PyQt.QtWidgets import (
    QAction, QHBoxLayout,QFileDialog, 
    QDialog, QVBoxLayout, QFormLayout, QLineEdit, 
    QPushButton, QLabel, QCheckBox,  QMessageBox, QTabWidget, QWidget
)

from qgis.PyQt.QtCore import QUrl, QByteArray
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply

from qgis.core import (
    QgsVectorLayer,
    QgsRasterLayer,
    QgsProject,
    QgsLayerTreeGroup,
    QgsSingleSymbolRenderer,
    QgsFillSymbol,
    QgsLineSymbol,
    QgsLayerMetadata,
    QgsBox3d,
    QgsDateTimeRange,
    QgsAuthMethodConfig,
    QgsApplication,
    Qgis,
    QgsCoordinateTransform,
    QgsMarkerSymbol,
    QgsSettings,
    QgsDataSourceUri,
    QgsMapLayer,
    QgsNetworkAccessManager,
)

import hashlib
import glob
import cProfile
import pstats
import io
import concurrent.futures

# Import infrastructure components
from .infrastructure.config import ConfigManager
from .infrastructure.auth import AuthManager
from .infrastructure.network import NetworkClient, MetadataClient

# Import domain components
from .domain.metadata import MetadataProcessor

# Import services
from .services.metadata_service import MetadataService
from .services.style_service import StyleService
from .services.layer_service import LayerService

from .tools import setup_logging

# --- PLUGIN_CODE_VERSION calculation updated to include all local code files ---
def _get_combined_code_hash():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    py_files = sorted(glob.glob(os.path.join(base_dir, '**/*.py'), recursive=True))
    md5 = hashlib.md5()
    for fname in py_files:
        with open(fname, 'rb') as f:
            md5.update(f.read())
    return md5.hexdigest()[:8]

PLUGIN_CODE_VERSION = _get_combined_code_hash()

class QGISPlugin:
    """
    QGIS Plugin for WindScout Grunddaten

    This plugin provides a QGIS interface for loading layers from external OGC services
    and internal TinyOWS services. It uses a clean architecture with dependency injection
    to provide robust and maintainable code.
    """
    
    def __init__(self, iface):
        """
        Initialize the plugin.
        
        Args:
            iface: QGIS interface
        """
        self.iface = iface
        self.logger = setup_logging(level=logging.INFO, tag='WS Grunddaten')
        self.title = 'WindScout Grunddaten Dienst'
        
        # Initialize infrastructure components
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
        self.config_manager = ConfigManager(config_path)
        self.auth_manager = AuthManager(self.logger)
        self.network_client = NetworkClient(self.config_manager, self.auth_manager)
        self.metadata_client = MetadataClient(self.network_client)
        
        # Initialize services
        self.metadata_service = MetadataService(self.config_manager, self.metadata_client)
        self.style_service = StyleService(self.config_manager)
        self.layer_service = LayerService(
            self.config_manager,
            self.auth_manager,
            self.metadata_service,
            self.style_service
        )
        
        # Set QGIS interface for layer service
        self.layer_service.set_iface(self.iface)
        
        # Initialize settings with defaults
        settings = QgsSettings()
        if not settings.contains("ogc_layer_handler/auto_load_styles"):
            settings.setValue("ogc_layer_handler/auto_load_styles", True)
        if not settings.contains("ogc_layer_handler/profiling_enabled"):
            settings.setValue("ogc_layer_handler/profiling_enabled", False)

    def profile_load_layers(self):
        """Profile the load_layers method and save results to a file."""
        # Check if profiling is enabled in settings
        settings = QgsSettings()
        profiling_enabled = settings.value("ogc_layer_handler/profiling_enabled", False, type=bool)
    
        if not profiling_enabled:
            # If profiling is disabled, just call load_layers directly
            self.load_layers()
            return
        try:
            # Create a profile object
            pr = cProfile.Profile()
            
            # Start profiling
            pr.enable()
            
            # Run the load_layers method
            self.load_layers()
            
            # Stop profiling
            pr.disable()
            
            # Create a StringIO object to capture the stats
            s = io.StringIO()
            
            # Sort stats by cumulative time
            ps = pstats.Stats(pr, stream=s).sort_stats('cumulative')
            
            # Print stats to StringIO object
            ps.print_stats()
            
            # Get the current timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            
            # Save to a file in the plugin directory
            profile_dir = os.path.join(os.path.dirname(__file__), 'profiles')
            os.makedirs(profile_dir, exist_ok=True)
            profile_file = os.path.join(profile_dir, f'load_layers_profile_{timestamp}.txt')
            
            with open(profile_file, 'w') as f:
                f.write(s.getvalue())
            
            self.logger.info(f"Profile saved to: {profile_file}")
            self.iface.messageBar().pushMessage(
                "Success", 
                f"Profile saved to: {profile_file}", 
                level=Qgis.Info
            )
            
        except Exception as e:
            self.logger.error(f"Error during profiling: {str(e)}")
            self.logger.error(traceback.format_exc())
            self.iface.messageBar().pushMessage(
                "Error", 
                f"Error during profiling: {str(e)}", 
                level=Qgis.Critical
            )

    def initGui(self):
        """Initialize the plugin GUI."""
        # Create actions
        self.load_action = QAction('Grundaten (neu) laden', self.iface.mainWindow())
        self.configure_action = QAction('Konfigurieren', self.iface.mainWindow())
        self.test_auth_action = QAction('Authentifizierung testen', self.iface.mainWindow())
        
        # Add export/import style actions
        self.export_styles_action = QAction('Stile exportieren', self.iface.mainWindow())
        self.import_styles_action = QAction('Stile importieren', self.iface.mainWindow())
        self.load_server_styles_action = QAction('Stile vom Server laden', self.iface.mainWindow())
        
        # Add version display action
        self.version_action = QAction(f'Code Version: {PLUGIN_CODE_VERSION}', self.iface.mainWindow())
        
        # Connect actions to methods
        self.load_action.triggered.connect(self.profile_load_layers)
        self.configure_action.triggered.connect(self.configure_server)
        self.test_auth_action.triggered.connect(self.test_auth_config)
        self.export_styles_action.triggered.connect(self.export_styles)
        self.import_styles_action.triggered.connect(self.import_styles)
        self.load_server_styles_action.triggered.connect(self.load_server_styles)
        self.version_action.triggered.connect(self.show_version)
        
        # Add actions to the Web menu
        self.iface.addPluginToWebMenu(f'&{self.title}', self.load_action)
        self.iface.addPluginToWebMenu(f'&{self.title}', self.configure_action)
        self.iface.addPluginToWebMenu(f'&{self.title}', self.test_auth_action)
        self.iface.addPluginToWebMenu(f'&{self.title}', self.export_styles_action)
        self.iface.addPluginToWebMenu(f'&{self.title}', self.import_styles_action)
        self.iface.addPluginToWebMenu(f'&{self.title}', self.load_server_styles_action)
        self.iface.addPluginToWebMenu(f'&{self.title}', self.version_action)
        
        # Add toolbar items
        self.toolbar = self.iface.addToolBar(f'{self.title}')
        self.toolbar.addAction(self.load_action)
        
        # Try to load pre-configured credentials
        if not self.auth_manager.has_credentials():
            if self.auth_manager.load_preconfigured_credentials():
                self.logger.info("Successfully loaded pre-configured credentials")
                self.iface.messageBar().pushMessage(
                    "Info", 
                    "Loaded pre-configured OGC credentials", 
                    level=Qgis.Info
                )
            else:
                self.logger.info("No pre-configured credentials found or failed to load them")

    def test_auth_config(self):
        """Test the authentication configuration by making a request to the server."""
        try:
            # Get authentication configuration ID
            auth_config_id = self.auth_manager.get_auth_config_id()
            if not auth_config_id:
                self.iface.messageBar().pushMessage(
                    "Error", 
                    "No authentication configuration found. Please configure API key first.", 
                    level=Qgis.Warning
                )
                return
            
            # Make test request to server
            test_path = "/qgis_config"
            success, response, status_code = self.network_client.request(test_path)
            
            if success:
                self.iface.messageBar().pushMessage(
                    "Success", 
                    f"Authentication test successful! Response size: {len(str(response))} bytes", 
                    level=Qgis.Success
                )
                self.logger.info(f"Authentication test successful. Status code: {status_code}")
            else:
                self.iface.messageBar().pushMessage(
                    "Error", 
                    f"Authentication test failed: {response.get('error', 'Unknown error')} (HTTP {status_code})", 
                    level=Qgis.Critical
                )
                self.logger.error(f"Authentication test failed: {response.get('error')}")
                self.logger.error(f"HTTP status code: {status_code}")
                
        except Exception as e:
            self.iface.messageBar().pushMessage(
                "Error", 
                f"Error testing authentication configuration: {str(e)}", 
                level=Qgis.Critical
            )
            self.logger.error(f"Error testing authentication configuration: {str(e)}")
            self.logger.error(traceback.format_exc())

    def configure_server(self):
        """Configure the server connection settings and API key authentication."""
        # Create a dialog
        dialog = QDialog(self.iface.mainWindow())
        dialog.setWindowTitle("OGC Server Configuration")
        dialog.resize(400, 350)  # Increased height for additional fields
        
        # Create tabs
        tabs = QTabWidget()
        server_tab = QWidget()
        auth_tab = QWidget()
        
        # Server tab
        server_layout = QVBoxLayout()
        server_form = QFormLayout()
        
        # Get existing settings
        hostname = self.config_manager.get_hostname()
        port = self.config_manager.get_port()
        
        # Get auto-load styles setting
        settings = QgsSettings()
        auto_load_styles = settings.value("ogc_layer_handler/auto_load_styles", True, type=bool)
        
        # Get layer filter setting
        layer_filter = settings.value("ogc_layer_handler/layer_filter", "", type=str)
        
        # Create fields
        hostname_input = QLineEdit(hostname)
        port_input = QLineEdit(port)
        layer_filter_input = QLineEdit(layer_filter)
        layer_filter_input.setToolTip("Enter comma-separated layer IDs to only load specific layers")
        
        # Add fields to form
        server_form.addRow("Hostname:", hostname_input)
        server_form.addRow("Port:", port_input)
        server_form.addRow("Layer Filter (IDs):", layer_filter_input)
        
        # Create auto-load styles checkbox
        auto_styles_checkbox = QCheckBox("Automatically load styles from server when loading layers")
        auto_styles_checkbox.setChecked(auto_load_styles)
        auto_styles_checkbox.setToolTip("When enabled, styles will be automatically loaded from server after layers are loaded")
        
        # Add form to layout
        server_layout.addLayout(server_form)
        server_layout.addWidget(auto_styles_checkbox)
        
        # Set layout for server tab
        server_tab.setLayout(server_layout)
        
        # Authentication tab
        auth_layout = QVBoxLayout()
        auth_form = QFormLayout()
        
        # Get existing credentials
        organization, api_key, save_key, auth_config_id = self.auth_manager.get_credentials()
        
        # Create fields
        organization_input = QLineEdit(organization)
        api_key_input = QLineEdit(api_key)
        
        # Add auth config ID info if available
        if auth_config_id:
            auth_config_label = QLabel(f"Authentication Configuration ID: {auth_config_id}")
            auth_layout.addWidget(auth_config_label)
        
        # Save password checkbox
        save_key_checkbox = QCheckBox("Save API key in QGIS Auth Database")
        save_key_checkbox.setChecked(save_key)
        
        # Add fields to form
        auth_form.addRow("Organization:", organization_input)
        auth_form.addRow("API Key:", api_key_input)
        
        # Add form and checkbox to layout
        auth_layout.addLayout(auth_form)
        auth_layout.addWidget(save_key_checkbox)
        
        # Set layout for auth tab
        auth_tab.setLayout(auth_layout)
        
        # Add tabs to tab widget
        tabs.addTab(server_tab, "Server")
        tabs.addTab(auth_tab, "Authentication")
        
        # Create buttons
        button_layout = QHBoxLayout()
        save_button = QPushButton("Save")
        cancel_button = QPushButton("Cancel")
        
        # Add buttons to layout
        button_layout.addWidget(save_button)
        button_layout.addWidget(cancel_button)
        
        # Create main layout
        main_layout = QVBoxLayout()
        main_layout.addWidget(tabs)
        main_layout.addLayout(button_layout)
        
        # Set layout for dialog
        dialog.setLayout(main_layout)
        
        # Create profiling checkbox
        profiling_enabled = settings.value("ogc_layer_handler/profiling_enabled", False, type=bool)
        profiling_checkbox = QCheckBox("Enable Performance Profiling")
        profiling_checkbox.setChecked(profiling_enabled)
        server_form.addRow("Profiling:", profiling_checkbox)
        
        # Connect buttons
        def save_settings():
            try:
                # Save server settings
                settings = QgsSettings()
                settings.setValue("ogc_layer_handler/config_hostname", hostname_input.text())
                settings.setValue("ogc_layer_handler/config_port", port_input.text())
                settings.setValue("ogc_layer_handler/auto_load_styles", auto_styles_checkbox.isChecked())
                settings.setValue("ogc_layer_handler/layer_filter", layer_filter_input.text())
                settings.setValue("ogc_layer_handler/profiling_enabled", profiling_checkbox.isChecked())
                
                # Save credentials
                organization = organization_input.text()
                api_key = api_key_input.text()
                save_key = save_key_checkbox.isChecked()
                
                self.auth_manager.save_credentials(organization, api_key, save_key)
                
                self.iface.messageBar().pushMessage("Success", "Server configuration saved", level=Qgis.Success)
                dialog.accept()
            except Exception as e:
                self.logger.error(f"Error saving settings: {str(e)}")
                self.iface.messageBar().pushMessage("Error", f"Failed to save settings: {str(e)}", level=Qgis.Critical)
        
        save_button.clicked.connect(save_settings)
        cancel_button.clicked.connect(dialog.reject)
        
        # Show the dialog
        dialog.exec_()
            
    def export_styles(self):
        """Export all layer styles to a JSON file"""
        try:
            self.logger.info("Exporting layer styles...")
            
            # Use style service to export styles
            if self.style_service.export_styles_to_file(self.iface.mainWindow()):
                self.iface.messageBar().pushMessage(
                    "Success",
                    "Layer styles exported successfully",
                    level=Qgis.Success
                )
            else:
                self.iface.messageBar().pushMessage(
                    "Info",
                    "Style export was cancelled or failed",
                    level=Qgis.Info
                )
                
        except Exception as e:
            self.logger.error(f"Error exporting styles: {str(e)}")
            self.logger.error(traceback.format_exc())
            self.iface.messageBar().pushMessage(
                "Error",
                f"Error exporting styles: {str(e)}",
                level=Qgis.Critical
            )
            
    def import_styles(self):
        """Import layer styles from a JSON file"""
        try:
            self.logger.info("Importing layer styles...")
            
            # Use style service to import styles
            if self.style_service.import_styles_from_file(self.iface.mainWindow()):
                self.iface.messageBar().pushMessage(
                    "Success",
                    "Layer styles imported and applied successfully",
                    level=Qgis.Success
                )
            else:
                self.iface.messageBar().pushMessage(
                    "Info",
                    "Style import was cancelled or failed",
                    level=Qgis.Info
                )
                
        except Exception as e:
            self.logger.error(f"Error importing styles: {str(e)}")
            self.logger.error(traceback.format_exc())
            self.iface.messageBar().pushMessage(
                "Error",
                f"Error importing styles: {str(e)}",
                level=Qgis.Critical
            )
            
    def load_server_styles(self):
        """Load styles from server for all loaded layers"""
        try:
            self.logger.info("Loading styles from server...")
            
            # Make sure network client is set in style service
            self.style_service.set_network_client(self.network_client)
            
            # Load styles from server
            if self.style_service.load_styles_from_server():
                self.iface.messageBar().pushMessage(
                    "Success",
                    "Styles loaded successfully from server",
                    level=Qgis.Success
                )
            else:
                self.iface.messageBar().pushMessage(
                    "Warning",
                    "Failed to load styles from server",
                    level=Qgis.Warning
                )
                
        except Exception as e:
            self.logger.error(f"Error loading server styles: {str(e)}")
            self.logger.error(traceback.format_exc())
            self.iface.messageBar().pushMessage(
                "Error",
                f"Error loading server styles: {str(e)}",
                level=Qgis.Critical
            )
            
    def show_version(self):
        """Show plugin version information"""
        QMessageBox.information(
            self.iface.mainWindow(),
            "Plugin Version",
            f"WindScout Grunddaten Plugin\nCode Version: {PLUGIN_CODE_VERSION}\nGithub: https://github.com/WindScout/renewable_basedata_plugin"
        )
        
    def load_layers(self):
        """Load layers from configuration."""
        try:
            self.logger.info("Starting layer loading")
            
            # Build the layer tree using layer service
            self.layer_service.build_layer_tree()
            
            # Auto-load styles if enabled in settings
            settings = QgsSettings()
            auto_load_styles = settings.value("ogc_layer_handler/auto_load_styles", True, type=bool)
            
            if auto_load_styles:
                self.logger.info("Auto-loading styles from server...")
                # Make sure network client is set in style service
                self.style_service.set_network_client(self.network_client)
                # Load styles from server
                self.style_service.load_styles_from_server()
            
            # Show success message
            self.iface.messageBar().pushMessage(
                "Success", 
                "Layers loaded successfully", 
                level=Qgis.Success
            )
            
        except Exception as e:
            self.logger.error(f"Error loading layers: {str(e)}")
            self.logger.error(traceback.format_exc())
            self.iface.messageBar().pushMessage(
                "Error", 
                f"Error loading layers: {str(e)}", 
                level=Qgis.Critical
            )

    def unload(self):
        """Removes the plugin menu items and icons from QGIS GUI."""
        # Remove the plugin menu items and icons
        self.iface.removePluginWebMenu(f'&{self.title}', self.load_action)
        self.iface.removePluginWebMenu(f'&{self.title}', self.configure_action)
        self.iface.removePluginWebMenu(f'&{self.title}', self.test_auth_action)
        self.iface.removePluginWebMenu(f'&{self.title}', self.export_styles_action)
        self.iface.removePluginWebMenu(f'&{self.title}', self.import_styles_action)
        self.iface.removePluginWebMenu(f'&{self.title}', self.load_server_styles_action)
        self.iface.removePluginWebMenu(f'&{self.title}', self.version_action)
        
        # Remove the toolbar
        if hasattr(self, 'toolbar'):
            del self.toolbar
