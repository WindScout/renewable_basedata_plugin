def classFactory(iface):
    from .plugin import QGISPlugin
    return QGISPlugin(iface)