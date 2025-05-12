from queue import Queue
import time
import logging
from typing import Optional, Dict, Any
import hashlib
import os
import pickle

from qgis.core import QgsNetworkAccessManager, QgsNetworkReplyContent
from qgis.PyQt.QtCore import QUrl, QByteArray
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply

class ConnectionPool:
    """Connection pool optimized for medium-speed connections like 4G"""
    
    def __init__(self, max_size: int = 8, timeout: int = 5):
        self.pool = Queue(maxsize=max_size)
        self.timeout = timeout
        self.logger = logging.getLogger(__name__)
        self._fill_pool()
        
    def _fill_pool(self):
        """Initialize the connection pool with network managers"""
        while not self.pool.full():
            # Using QgsNetworkAccessManager instead of requests.Session
            # Since QgsNetworkAccessManager is a singleton, we'll store
            # the main instance with some additional metadata for tracking
            network_manager = {
                'manager': QgsNetworkAccessManager.instance(),
                'created_at': time.time()
            }
            self.pool.put(network_manager)
            
    def get_manager(self) -> Dict:
        """Get a network manager from the pool with timeout"""
        try:
            return self.pool.get(timeout=self.timeout)
        except:
            self.logger.warning("Connection pool timeout, creating new manager")
            return {
                'manager': QgsNetworkAccessManager.instance(),
                'created_at': time.time()
            }
            
    def return_manager(self, manager_dict: Dict):
        """Return a manager to the pool"""
        try:
            # Check if manager is older than 5 minutes, if so create a new one
            if time.time() - manager_dict.get('created_at', 0) > 300:  # 5 minutes
                # Just get a fresh instance instead
                self.pool.put({
                    'manager': QgsNetworkAccessManager.instance(),
                    'created_at': time.time()
                })
            else:
                self.pool.put(manager_dict, block=False)
        except:
            # Pool is full, just let the reference go
            pass

class QgisResponse:
    """Simple wrapper to provide compatibility with requests Response objects"""
    
    def __init__(self, reply_content: QgsNetworkReplyContent):
        self.reply_content = reply_content
        self._text = None
        self._content = None
        self.status_code = reply_content.error() == QNetworkReply.NoError and 200 or 0
        
        # Map error codes to HTTP status codes
        if self.status_code == 0:
            error_code = reply_content.error()
            if error_code == QNetworkReply.ConnectionRefusedError:
                self.status_code = 503  # Service Unavailable
            elif error_code == QNetworkReply.AuthenticationRequiredError:
                self.status_code = 401  # Unauthorized
            elif error_code == QNetworkReply.ContentNotFoundError:
                self.status_code = 404  # Not Found
            elif error_code == QNetworkReply.TimeoutError:
                self.status_code = 408  # Request Timeout
            else:
                self.status_code = 500  # Generic server error
                
        # Get HTTP status code if available in attributes
        http_status = reply_content.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        if http_status:
            self.status_code = http_status
        
        # Headers
        self.headers = {}
        for header in reply_content.rawHeaderList():
            header_name = bytes(header).decode('utf-8')
            header_value = bytes(reply_content.rawHeader(header)).decode('utf-8')
            self.headers[header_name] = header_value
    
    @property
    def text(self) -> str:
        """Get response text"""
        if self._text is None:
            content = self.reply_content.content()
            self._text = bytes(content).decode('utf-8')
        return self._text
    
    @property
    def content(self) -> bytes:
        """Get response content as bytes"""
        if self._content is None:
            self._content = bytes(self.reply_content.content())
        return self._content
        
    def json(self) -> Dict:
        """Parse response content as JSON"""
        import json
        return json.loads(self.text)
        
    def raise_for_status(self):
        """Raise an exception if status code indicates an error"""
        if self.status_code >= 400:
            raise Exception(f"HTTP Error {self.status_code}")

class ConnectionManager:
    """Manages connections and caching for network requests"""
    
    def __init__(self, cache_dir: str = None):
        self.pool = ConnectionPool()
        self.cache_dir = cache_dir or os.path.join(os.path.expanduser('~'), '.qgis_cache')
        os.makedirs(self.cache_dir, exist_ok=True)
        self.logger = logging.getLogger(__name__)
        self._connection_quality = None
        self._last_quality_check = 0
        
    def detect_connection_quality(self, force: bool = False) -> str:
        """
        Detect connection quality and cache result for 5 minutes
        Returns: "SLOW", "MEDIUM", or "FAST"
        """
        now = time.time()
        if not force and self._connection_quality and (now - self._last_quality_check) < 300:
            return self._connection_quality
            
        try:
            start = time.time()
            manager_dict = self.pool.get_manager()
            manager = manager_dict['manager']
            
            try:
                # Create request
                request = QNetworkRequest(QUrl("https://www.google.com"))
                request.setHeader(QNetworkRequest.UserAgentHeader, "QGIS Network Test")
                
                # Make sync request
                reply = manager.blockingGet(request)
                
                # Check if request was successful
                if reply.error() == QNetworkReply.NoError:
                    latency = time.time() - start
                    
                    if latency > 0.5:  # High latency
                        quality = "SLOW"
                    elif latency > 0.2:
                        quality = "MEDIUM"
                    else:
                        quality = "FAST"
                        
                    self._connection_quality = quality
                    self._last_quality_check = now
                    return quality
                else:
                    self.logger.warning(f"Connection quality check failed: {reply.errorString()}")
                    return "SLOW"
                    
            finally:
                self.pool.return_manager(manager_dict)
        except Exception as e:
            self.logger.warning(f"Connection quality check failed: {str(e)}")
            return "SLOW"
            
    def fetch_with_cache(self, url: str, headers: Optional[Dict] = None, 
                        cache_ttl: int = 300) -> QgisResponse:
        """
        Fetch URL with caching for slow connections
        
        Args:
            url: URL to fetch
            headers: Optional request headers
            cache_ttl: Cache time-to-live in seconds
            
        Returns:
            QgisResponse: Response data
        """
        # Generate cache key from URL and headers
        cache_key = hashlib.md5(
            (url + str(sorted((headers or {}).items()))).encode()
        ).hexdigest()
        cache_file = os.path.join(self.cache_dir, cache_key)
        
        # Check cache first
        if os.path.exists(cache_file):
            if time.time() - os.path.getmtime(cache_file) < cache_ttl:
                try:
                    with open(cache_file, 'rb') as f:
                        return pickle.load(f)
                except:
                    self.logger.warning("Cache read failed, fetching fresh")
        
        # Add default headers
        headers = headers or {}
        if 'Accept-Encoding' not in headers:
            headers['Accept-Encoding'] = 'gzip, deflate'
        
        # Get connection quality-based timeout
        quality = self.detect_connection_quality()
        timeout = 5000 if quality == "SLOW" else 10000  # in milliseconds for Qt
        
        # Make request
        manager_dict = self.pool.get_manager()
        manager = manager_dict['manager']
        
        try:
            # Create request object
            request = QNetworkRequest(QUrl(url))
            
            # Set timeout
            request.setAttribute(QNetworkRequest.CacheLoadControlAttribute, QNetworkRequest.PreferNetwork)
            request.setAttribute(QNetworkRequest.RedirectPolicyAttribute, QNetworkRequest.NoLessSafeRedirectPolicy)
            
            # Set headers
            for header_name, header_value in headers.items():
                request.setRawHeader(
                    QByteArray(header_name.encode()), 
                    QByteArray(str(header_value).encode())
                )
            
            # Make blocking request
            reply = manager.blockingGet(request, timeoutMs=timeout)
            
            # Create response object
            response = QgisResponse(reply)
            
            # Cache successful response
            if response.status_code < 400:
                try:
                    with open(cache_file, 'wb') as f:
                        pickle.dump(response, f)
                except:
                    self.logger.warning("Failed to cache response")
                    
            return response
            
        finally:
            self.pool.return_manager(manager_dict) 