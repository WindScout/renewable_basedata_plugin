"""
Style service for WindScout Grunddaten Plugin

This service handles layer styling operations.
"""
import logging
from typing import Dict, Optional, List, Tuple
import json
import os
from io import StringIO
import base64
from datetime import datetime

from qgis.core import (
    QgsSingleSymbolRenderer,
    QgsFillSymbol,
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsRasterLayer,
    QgsVectorLayer,
    QgsProject,
    QgsMapLayer
)
from qgis.PyQt.QtCore import QUrl, QTemporaryFile, QByteArray
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply
from qgis.PyQt.QtXml import QDomDocument
from qgis.PyQt.QtWidgets import QFileDialog

from ..infrastructure.config import ConfigManager


class StyleService:
    """
    Service for handling layer styling operations
    
    This service applies styles to layers based on configuration.
    """
    
    def __init__(self, config_manager: ConfigManager):
        """
        Initialize the style service.
        
        Args:
            config_manager: Configuration manager
        """
        self.logger = logging.getLogger('qgis_plugin.style_service')
        self.config_manager = config_manager
        self.network_client = None
        self.server_styles = {}

    def set_network_client(self, network_client):
        """
        Set the network client for server requests.
        
        Args:
            network_client: Network client
        """
        self.network_client = network_client
        
    def load_styles_from_server(self) -> bool:
        """
        Load styles from server.
        
        Returns:
            bool: True if styles were successfully loaded
        """
        try:
            if not self.network_client:
                self.logger.error("Network client not set, cannot load styles from server")
                return False
                
            # Request styles from server
            success, response, status_code = self.network_client.request('/styles')
            
            if not success:
                self.logger.error(f"Failed to load styles from server: HTTP {status_code}")
                return False
                
            if not isinstance(response, dict):
                self.logger.error("Invalid styles response format from server")
                return False
                
            # Parse the nested styles structure
            if 'styles' not in response:
                self.logger.error("No styles found in server response")
                return False
                
            # Extract styles from the nested structure
            styles_dict = response.get('styles', {})
            self.logger.info(f"Loaded style groups: {list(styles_dict.keys())}")
            
            # Store the full styles response
            self.server_styles_response = response
            
            # Flatten the styles structure for easier lookup
            self.server_styles = {}
            
            # Process each style group
            for group_id, group_styles in styles_dict.items():
                for layer_id, layer_style in group_styles.items():
                    # Create a combined key for uniqueness
                    style_key = f"{group_id}:{layer_id}"
                    
                    # Store style with the QML content
                    if 'qml_content' in layer_style:
                        self.server_styles[style_key] = {
                            'group': group_id,
                            'layer': layer_id,
                            'qml_content': layer_style['qml_content']
                        }
                        self.logger.debug(f"Stored QML style for {style_key}")
                    else:
                        self.logger.warning(f"No QML content found for style {style_key}")
            
            self.logger.info(f"Successfully loaded {len(self.server_styles)} layer styles from server")
            
            # Apply styles to all loaded layers
            self.apply_styles_to_layers()
            
            return True
        except Exception as e:
            self.logger.error(f"Error loading styles from server: {str(e)}")
            import traceback
            self.logger.error(traceback.format_exc())
            return False

    def apply_styles_to_layers(self):
        """Apply loaded server styles to all layers in the project"""
        project = QgsProject.instance()
        
        # Get all map layers
        all_layers = project.mapLayers()
        styled_count = 0
        
        self.logger.info(f"Applying styles to {len(all_layers)} layers")
        
        for layer_id, layer in all_layers.items():
            try:
                # Try to find a matching style
                style_applied = self._apply_server_style_to_layer(layer)
                if style_applied:
                    styled_count += 1
            except Exception as e:
                self.logger.warning(f"Error applying server style to layer {layer.name()}: {str(e)}")
                
        self.logger.info(f"Applied server styles to {styled_count} layers")
        
        # Refresh the entire layer tree after all styles are applied
        self._refresh_entire_layer_tree()
        
    def _apply_server_style_to_layer(self, layer) -> bool:
        """
        Try to find and apply a server style to a layer.
        
        Args:
            layer: QGIS layer
            
        Returns:
            bool: True if a style was applied
        """
        if not isinstance(layer, QgsMapLayer) or not self.server_styles:
            return False
        
        # Get layer properties to match with styles
        source_id = layer.customProperty('source_id', '')
        type_name = layer.customProperty('type_name', '')
        layer_name = layer.name()
        
        # If source_id is empty, try to extract from name
        if not source_id:
            # Some heuristics to extract potential IDs from layer names
            parts = layer_name.split(':')
            if len(parts) > 1:
                source_id = parts[-1].strip()
                
        # Try different matching strategies
        potential_matches = []
        
        # 1. Direct match by layer name (lowest priority)
        for style_key, style_data in self.server_styles.items():
            if style_data['layer'] == layer_name:
                potential_matches.append((style_key, 1))  # Priority 1
                
        # 2. Match by type_name if available (medium priority)
        if type_name:
            for style_key, style_data in self.server_styles.items():
                if style_data['layer'] == type_name:
                    potential_matches.append((style_key, 2))  # Priority 2
                    
        # 3. Match by source_id if available (highest priority)
        if source_id:
            for style_key, style_data in self.server_styles.items():
                if style_data['layer'] == source_id:
                    potential_matches.append((style_key, 3))  # Priority 3
                    
        # Get the highest priority match
        if potential_matches:
            # Sort by priority (highest last)
            potential_matches.sort(key=lambda x: x[1])
            best_match = potential_matches[-1][0]
            
            # Apply the matching style
            style_data = self.server_styles[best_match]
            success = self._apply_qml_style(layer, style_data['qml_content'])
            
            if success:
                self.logger.info(f"Applied server style '{best_match}' to layer '{layer.name()}'")
                return True
            else:
                self.logger.warning(f"Failed to apply server style '{best_match}' to layer '{layer.name()}'")
                
        return False
        
    def _apply_qml_style(self, layer, qml_base64: str) -> bool:
        """
        Apply QML style content to a layer.
        
        Args:
            layer: QGIS layer
            qml_base64: QML style content as base64 encoded string
            
        Returns:
            bool: True if style was successfully applied
        """
        try:
            # Create a temporary file for the QML content
            temp_file = QTemporaryFile()
            if temp_file.open():
                # Decode base64 directly to bytes and write
                temp_file.write(base64.b64decode(qml_base64))
                temp_file.flush()
                
                # Load style from the temp file
                message, result = layer.loadNamedStyle(temp_file.fileName(), categories = QgsMapLayer.Symbology)
                temp_file.close()
                
                if result:
                    # Trigger repaint and refresh layer tree
                    layer.triggerRepaint()
                    self._refresh_layer_tree_symbology(layer)
                    return True
                else:
                    self.logger.warning(f"Failed to load style: {message}")
                    return False
            else:
                self.logger.error("Could not open temporary file for QML content")
                return False
                
        except Exception as e:
            self.logger.error(f"Error applying QML style: {str(e)}")
            import traceback
            self.logger.error(traceback.format_exc())
            return False
        
    def _refresh_layer_tree_symbology(self, layer):
        """
        Refresh the layer tree to show updated symbology.
        
        Args:
            layer: QGIS layer that was styled
        """
        try:
            # Get the layer tree root
            root = QgsProject.instance().layerTreeRoot()
            
            # Find the layer node in the tree
            layer_node = root.findLayer(layer.id())
            if layer_node:
                # Force refresh of the layer tree node
                layer_node.setItemVisibilityChecked(layer_node.itemVisibilityChecked())
                
            # Also refresh the entire layer tree model
            from qgis.utils import iface
            if iface and hasattr(iface, 'layerTreeView'):
                layer_tree_view = iface.layerTreeView()
                if layer_tree_view and hasattr(layer_tree_view, 'refreshLayerSymbology'):
                    layer_tree_view.refreshLayerSymbology(layer.id())
                elif layer_tree_view and hasattr(layer_tree_view, 'model'):
                    # Force model refresh
                    model = layer_tree_view.model()
                    if model:
                        model.refreshLayerLegend(layer_node)
                        
        except Exception as e:
            self.logger.warning(f"Could not refresh layer tree symbology: {str(e)}")

    def apply_style(self, layer, layer_id: str, type_name: str = "") -> None:
        """
        Apply style to a layer.
        
        Args:
            layer: QGIS layer to style
            layer_id: Layer ID
            type_name: Type name for fallback style matching
        """
        # Skip styling for invalid layers
        if not layer or not layer.isValid():
            return
            
        # Check if styling should be deferred (layer not yet in layer tree or explicitly marked)
        should_defer = False
        if isinstance(layer, QgsMapLayer) and layer.customProperty('defer_styling'):
            should_defer = True
        else:
            root = QgsProject.instance().layerTreeRoot()
            node = root.findLayer(layer.id()) if hasattr(layer, 'id') else None
            should_defer = not node
        
        if should_defer:
            layer.setCustomProperty('pending_style', f"{layer_id}|{type_name}")
            layer.setCustomProperty('source_id', layer_id)
            if type_name:
                layer.setCustomProperty('type_name', type_name)
            return
            
        # Store reference IDs as custom properties for later style matching
        layer.setCustomProperty('source_id', layer_id)
        if type_name:
            layer.setCustomProperty('type_name', type_name)
            
        # First try to apply server style
        if hasattr(self, 'server_styles') and self.server_styles:
            if self._apply_server_style_to_layer(layer):
                return
        
        # If no server style applied, fall back to local config style
        style_config = self.config_manager.get_style_config(layer_id, type_name)
        if not style_config:
            self.logger.debug(f"No style configuration found for layer {layer_id}")
            return
            
        # Apply style based on type
        style_type = style_config.get('type')
        
        if style_type == 'fill':
            self._apply_fill_style(layer, style_config)
        elif style_type == 'line':
            self._apply_line_style(layer, style_config)
        elif style_type == 'marker':
            self._apply_marker_style(layer, style_config)
        else:
            self.logger.warning(f"Unknown style type: {style_type}")
            
    def _apply_fill_style(self, layer, style_config: Dict) -> None:
        """
        Apply fill symbol style to a layer.
        
        Args:
            layer: QGIS layer
            style_config: Style configuration
        """
        try:
            # Get colors from style config
            fill_color = style_config['color']
            outline_color = style_config['outline_color']

            # Create symbol with properties
            symbol = QgsFillSymbol.createSimple({
                'color': f"{fill_color[0]},{fill_color[1]},{fill_color[2]},{int(fill_color[3] * 255)}",
                'style': 'solid',
                'outline_width': '0.5',
                'outline_style': 'solid',
                'outline_color': f"{outline_color[0]},{outline_color[1]},{outline_color[2]},{int(outline_color[3] * 255)}"
            })
            
            renderer = QgsSingleSymbolRenderer(symbol)
            layer.setRenderer(renderer)
            
        except Exception as e:
            self.logger.error(f"Error applying fill style: {str(e)}")
            
    def _apply_line_style(self, layer, style_config: Dict) -> None:
        """
        Apply line symbol style to a layer.
        
        Args:
            layer: QGIS layer
            style_config: Style configuration
        """
        try:
            line_color = style_config['color']
            width = style_config.get('width', 0.5)

            symbol = QgsLineSymbol.createSimple({
                'line_color': f"{line_color[0]},{line_color[1]},{line_color[2]},{int(line_color[3] * 255)}",
                'line_width': str(width),
                'line_style': 'solid'
            })
            
            renderer = QgsSingleSymbolRenderer(symbol)
            layer.setRenderer(renderer)
            
        except Exception as e:
            self.logger.error(f"Error applying line style: {str(e)}")
            
    def _apply_marker_style(self, layer, style_config: Dict) -> None:
        """
        Apply marker symbol style to a layer.
        
        Args:
            layer: QGIS layer
            style_config: Style configuration
        """
        try:
            fill_color = style_config['color']
            outline_color = style_config.get('outline_color', fill_color)
            size = style_config.get('size', 3)

            symbol = QgsMarkerSymbol.createSimple({
                'name': 'circle',
                'color': f"{fill_color[0]},{fill_color[1]},{fill_color[2]},{int(fill_color[3] * 255)}",
                'outline_color': f"{outline_color[0]},{outline_color[1]},{outline_color[2]},{int(outline_color[3] * 255)}",
                'size': str(size),
                'outline_style': 'solid',
                'outline_width': '0.5'
            })
            
            renderer = QgsSingleSymbolRenderer(symbol)
            layer.setRenderer(renderer)
            
        except Exception as e:
            self.logger.error(f"Error applying marker style: {str(e)}")
            
    def apply_deferred_styles(self, layers: list) -> None:
        """
        Apply styles to layers that were deferred during initial loading.
        
        Args:
            layers: List of QGIS layers
        """
        for layer in layers:
            if not layer.isValid():
                continue
                
            # Check if layer has pending style
            pending_style = layer.customProperty('pending_style')
            if pending_style:
                try:
                    # Format is "layer_id|type_name"
                    parts = pending_style.split('|')
                    layer_id = parts[0]
                    type_name = parts[1] if len(parts) > 1 else ""
                    
                    # Apply style
                    self.apply_style(layer, layer_id, type_name)
                    
                    # Remove pending style property
                    layer.removeCustomProperty('pending_style')
                    
                except Exception as e:
                    self.logger.error(f"Error applying deferred style: {str(e)}")
            
    def get_style_for_layer(self, layer_id: str, type_name: str = "") -> Optional[Dict]:
        """
        Get style configuration for a layer.
        
        Args:
            layer_id: Layer ID
            type_name: Type name for fallback style matching
            
        Returns:
            Dict: Style configuration or None if not found
        """
        # Try config manager first
        return self.config_manager.get_style_config(layer_id, type_name)

    def export_styles_to_file(self, parent_widget=None) -> bool:
        """
        Export all layer styles to a JSON file.
        
        Args:
            parent_widget: Parent widget for file dialog
            
        Returns:
            bool: True if export was successful
        """
        try:
            # Get file path from user
            file_path, _ = QFileDialog.getSaveFileName(
                parent_widget,
                "Export Layer Styles",
                "layer_styles.json",
                "JSON Files (*.json);;All Files (*)"
            )
            
            if not file_path:
                self.logger.info("Export cancelled by user")
                return False
                
            # Get all layers from project
            project = QgsProject.instance()
            all_layers = project.mapLayers()
            
            if not all_layers:
                self.logger.warning("No layers found in project")
                return False
                
            # Build export structure
            export_data = {
                "version": "1.0",
                "exported_date": datetime.now().isoformat(),
                "plugin": "windscout_grunddaten",
                "styles": {}
            }
            
            exported_count = 0
            
            for layer_id, layer in all_layers.items():
                try:
                    # Skip raster layers for now
                    if isinstance(layer, QgsRasterLayer):
                        continue
                        
                    # Get layer information
                    layer_name = layer.name()
                    source_id = layer.customProperty('source_id', '')
                    type_name = layer.customProperty('type_name', '')
                    
                    # Export the layer's current style as QML
                    qml_content = self._export_layer_qml(layer)
                    if qml_content:
                        # Determine grouping (use source service or 'custom')
                        group_name = "custom"
                        if source_id:
                            # Try to determine group from source_id
                            service_config = None
                            for service_id in ["mastr_energy", "grid_data", "geo_data"]:  # Common groups
                                if self.config_manager.get_service_config(service_id):
                                    layer_config = self.config_manager.get_layer_config(service_id, source_id)
                                    if layer_config:
                                        group_name = service_id
                                        break
                        
                        # Ensure group exists
                        if group_name not in export_data["styles"]:
                            export_data["styles"][group_name] = {}
                            
                        # Use source_id if available, otherwise layer name
                        style_key = source_id if source_id else layer_name
                        
                        # Store style data
                        export_data["styles"][group_name][style_key] = {
                            "layer_name": layer_name,
                            "layer_id": layer_id,
                            "source_id": source_id,
                            "type_name": type_name,
                            "qml_content": base64.b64encode(qml_content.encode('utf-8')).decode('ascii'),
                            "exported_at": datetime.now().isoformat()
                        }
                        
                        exported_count += 1
                        self.logger.debug(f"Exported style for layer: {layer_name}")
                        
                except Exception as e:
                    self.logger.warning(f"Failed to export style for layer {layer.name()}: {str(e)}")
                    continue
                    
            # Write to file
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(export_data, f, indent=2, ensure_ascii=False)
                
            self.logger.info(f"Successfully exported {exported_count} layer styles to {file_path}")
            return True
            
        except Exception as e:
            self.logger.error(f"Error exporting styles: {str(e)}")
            import traceback
            self.logger.error(traceback.format_exc())
            return False
            
    def _export_layer_qml(self, layer) -> Optional[str]:
        """
        Export a layer's style as QML string.
        
        Args:
            layer: QGIS layer
            
        Returns:
            str: QML content or None if export failed
        """
        try:
            # Create temporary file for QML export
            temp_file = QTemporaryFile()
            if temp_file.open():
                temp_file_path = temp_file.fileName()
                temp_file.close()
                
                # Export style to temporary file
                message, result = layer.saveNamedStyle(temp_file_path, categories = QgsMapLayer.Symbology)
                
                if result:
                    # Read QML content from file
                    with open(temp_file_path, 'r', encoding='utf-8') as f:
                        qml_content = f.read()
                    
                    # Clean up
                    try:
                        os.remove(temp_file_path)
                    except:
                        pass
                        
                    return qml_content
                else:
                    self.logger.warning(f"Failed to export layer style: {message}")
                    return None
            else:
                self.logger.error("Could not create temporary file for QML export")
                return None
                
        except Exception as e:
            self.logger.error(f"Error exporting layer QML: {str(e)}")
            return None

    def import_styles_from_file(self, parent_widget=None) -> bool:
        """
        Import layer styles from a JSON file.
        
        Args:
            parent_widget: Parent widget for file dialog
            
        Returns:
            bool: True if import was successful
        """
        try:
            # Get file path from user
            file_path, _ = QFileDialog.getOpenFileName(
                parent_widget,
                "Import Layer Styles",
                "",
                "JSON Files (*.json);;All Files (*)"
            )
            
            if not file_path:
                self.logger.info("Import cancelled by user")
                return False
                
            # Read file
            with open(file_path, 'r', encoding='utf-8') as f:
                import_data = json.load(f)
                
            # Validate structure
            if 'styles' not in import_data:
                self.logger.error("Invalid import file: no 'styles' key found")
                return False
                
            # Process styles similar to server loading
            imported_styles = {}
            
            # Process each style group
            for group_id, group_styles in import_data['styles'].items():
                for layer_id, layer_style in group_styles.items():
                    # Create a combined key for uniqueness
                    style_key = f"{group_id}:{layer_id}"
                    
                    # Store style with the QML content
                    if 'qml_content' in layer_style:
                        imported_styles[style_key] = {
                            'group': group_id,
                            'layer': layer_id,
                            'qml_content': layer_style['qml_content']
                        }
                        self.logger.debug(f"Imported QML style for {style_key}")
                    else:
                        self.logger.warning(f"No QML content found for style {style_key}")
            
            # Store imported styles (they will take precedence over server styles)
            self.imported_styles = imported_styles
            
            # Apply to current layers
            self._apply_imported_styles_to_layers()
            
            self.logger.info(f"Successfully imported {len(imported_styles)} layer styles from {file_path}")
            return True
            
        except Exception as e:
            self.logger.error(f"Error importing styles: {str(e)}")
            import traceback
            self.logger.error(traceback.format_exc())
            return False
            
    def _apply_imported_styles_to_layers(self):
        """Apply imported styles to all layers in the project"""
        if not hasattr(self, 'imported_styles') or not self.imported_styles:
            return
            
        project = QgsProject.instance()
        all_layers = project.mapLayers()
        styled_count = 0
        
        self.logger.info(f"Applying imported styles to {len(all_layers)} layers")
        
        for layer_id, layer in all_layers.items():
            try:
                # Try to find a matching imported style
                style_applied = self._apply_imported_style_to_layer(layer)
                if style_applied:
                    styled_count += 1
            except Exception as e:
                self.logger.warning(f"Error applying imported style to layer {layer.name()}: {str(e)}")
                
        self.logger.info(f"Applied imported styles to {styled_count} layers")
        
        # Refresh the entire layer tree after all styles are applied
        self._refresh_entire_layer_tree()
        
    def _apply_imported_style_to_layer(self, layer) -> bool:
        """
        Try to find and apply an imported style to a layer.
        
        Args:
            layer: QGIS layer
            
        Returns:
            bool: True if a style was applied
        """
        if not isinstance(layer, QgsMapLayer) or not hasattr(self, 'imported_styles') or not self.imported_styles:
            return False
        
        # Get layer properties to match with styles
        source_id = layer.customProperty('source_id', '')
        type_name = layer.customProperty('type_name', '')
        layer_name = layer.name()
        
        # Try different matching strategies (same as server styles)
        potential_matches = []
        
        # 1. Direct match by layer name (lowest priority)
        for style_key, style_data in self.imported_styles.items():
            if style_data['layer'] == layer_name:
                potential_matches.append((style_key, 1))
                
        # 2. Match by type_name if available (medium priority)
        if type_name:
            for style_key, style_data in self.imported_styles.items():
                if style_data['layer'] == type_name:
                    potential_matches.append((style_key, 2))
                    
        # 3. Match by source_id if available (highest priority)
        if source_id:
            for style_key, style_data in self.imported_styles.items():
                if style_data['layer'] == source_id:
                    potential_matches.append((style_key, 3))
                    
        # Get the highest priority match
        if potential_matches:
            # Sort by priority (highest last)
            potential_matches.sort(key=lambda x: x[1])
            best_match = potential_matches[-1][0]
            
            # Apply the matching style
            style_data = self.imported_styles[best_match]
            success = self._apply_qml_style(layer, style_data['qml_content'])
            
            if success:
                self.logger.info(f"Applied imported style '{best_match}' to layer '{layer.name()}'")
                return True
            else:
                self.logger.warning(f"Failed to apply imported style '{best_match}' to layer '{layer.name()}'")
                
        return False

    def _refresh_entire_layer_tree(self):
        """Refresh the entire layer tree to ensure all symbology is visible."""
        try:
            from qgis.utils import iface
            if iface and hasattr(iface, 'layerTreeView'):
                layer_tree_view = iface.layerTreeView()
                if layer_tree_view:
                    # Get the model and refresh it
                    model = layer_tree_view.model()
                    if model and hasattr(model, 'refreshLayerLegend'):
                        # Refresh legend for all layers
                        root = QgsProject.instance().layerTreeRoot()
                        for layer in QgsProject.instance().mapLayers().values():
                            layer_node = root.findLayer(layer.id())
                            if layer_node:
                                model.refreshLayerLegend(layer_node)
                                
                    # Force a full refresh of the view
                    layer_tree_view.model().layoutChanged.emit()
                    
        except Exception as e:
            self.logger.warning(f"Could not refresh entire layer tree: {str(e)}") 