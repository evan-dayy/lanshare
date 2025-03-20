"""This module implements file sharing functionality for the LAN sharing service."""

import os
import shutil
import threading
import time
from pathlib import Path
import json
import socket
import ftplib
from typing import Dict, List, Optional, Set, Tuple, Union
from datetime import datetime

from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

from .types import Peer
import logging

# Configure logging to write to a file
log_file_dir = Path.cwd()
log_file = log_file_dir / 'file_transfer_log.txt'

# If the log file already exists, delete it to start fresh
if log_file.exists():
    log_file.unlink() 

# Configure logging with the clean log file
logging.basicConfig(
    filename=str(log_file),
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('file_share')
logger.info("Starting new log session")

class SharedResource:
    """Represents a shared file or directory in the LAN sharing service.
    
    Attributes:
        id: Unique identifier for the shared resource.
        owner: Username of the owner.
        path: Original path of the resource on the owner's system.
        is_directory: Whether the resource is a directory.
        allowed_users: Set of usernames allowed to access the resource.
        shared_to_all: Whether the resource is shared to everyone.
        timestamp: When the resource was shared.
        ftp_password: Password for FTP access.
        modified_time: Last modification time of the original file/directory.
    """
    
    def __init__(self, 
                 owner: str, 
                 path: str, 
                 is_directory: bool = False, 
                 shared_to_all: bool = False,
                 ftp_password: str = None):
        """Initialize a SharedResource instance.
        Args:
            owner: Username of the resource owner.
            path: Path to the resource on the owner's system.
            is_directory: Whether the resource is a directory.
            shared_to_all: Whether the resource is shared to everyone.
            ftp_password: Password for FTP access.
        """
        basename = os.path.basename(path)
        if basename in ['.', '..']:
            # Get the actual directory name for resource ID generation
            basename = os.path.basename(os.path.abspath(path))
        
        self.id = f"{owner}_{int(time.time())}_{os.path.basename(path)}"
        self.owner = owner
        self.path = path
        self.is_directory = is_directory
        self.allowed_users = set()
        self.shared_to_all = shared_to_all
        self.timestamp = datetime.now()
        self.ftp_password = ftp_password
        
        # Get the modification time of the original file/directory
        try:
            self.modified_time = os.path.getmtime(path)
        except Exception:
            self.modified_time = time.time()
    
    def to_dict(self) -> Dict:
        return {
            'id': self.id,
            'owner': self.owner,
            'path': self.path,
            'is_directory': self.is_directory,
            'allowed_users': list(self.allowed_users),
            'shared_to_all': self.shared_to_all,
            'timestamp': self.timestamp.isoformat(),
            'ftp_password': self.ftp_password,
            'modified_time': self.modified_time
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'SharedResource':
        resource = cls(
            owner=data['owner'],
            path=data['path'],
            is_directory=data['is_directory'],
            shared_to_all=data['shared_to_all'],
            ftp_password=data.get('ftp_password')
        )
        resource.id = data['id']
        resource.allowed_users = set(data['allowed_users'])
        resource.timestamp = datetime.fromisoformat(data['timestamp'])
        resource.modified_time = data.get('modified_time', time.time())
        return resource
    
    def add_user(self, username: str) -> None:
        self.allowed_users.add(username)
    
    def remove_user(self, username: str) -> None:
        if username in self.allowed_users:
            self.allowed_users.remove(username)
    
    def can_access(self, username: str) -> bool:
        return (self.owner == username or 
                username in self.allowed_users or 
                self.shared_to_all)
    
    def update_modified_time(self, new_time: float) -> bool:
        if new_time != self.modified_time:
            self.modified_time = new_time
            return True
        return False


class FileShareManager:
    """Manages file sharing in the LAN sharing service.
    
    This class handles:
    1. Sharing files and directories with selected peers
    2. Managing access permissions
    3. Running an FTP server for file transfers
    4. Announcing shared resources to peers
    5. Downloading shared resources automatically
    6. Synchronizing file updates based on timestamps
    """
    
    def __init__(self, username: str, discovery_service):
        self.username = username
        self.discovery = discovery_service
        self.config = discovery_service.config
        
        # Directory where shared files are stored
        self.share_dir = Path.cwd() / 'shared'
        self.share_dir.mkdir(exist_ok=True)
        # Create directory for this user's shared files
        self.user_share_dir = self.share_dir / username
        self.user_share_dir.mkdir(exist_ok=True)
        # Tracking shared resources
        self.shared_resources: Dict[str, SharedResource] = {}
        self.received_resources: Dict[str, SharedResource] = {}
        
        # Download history to avoid downloading the same file multiple times
        self.downloaded_resources: Set[str] = set()
        
        # FTP server settings
        self.authorizer = DummyAuthorizer()
        self.ftp_handler = FTPHandler
        self.ftp_handler.authorizer = self.authorizer
        # Configure FTP handler for binary mode by default
        self.ftp_handler.use_encoding = 'utf-8'
        # Force binary mode for all file transfers
        self.ftp_handler.use_binary = True
        # Add the user to the authorizer with full permissions to their share directory
        self.default_password = "anonymous"  # Simplified password for easier testing
        self.authorizer.add_user(
            username, 
            password=self.default_password, 
            homedir=str(self.user_share_dir),
            perm='elradfmwMT'  # Full permissions
        )
        # Also add anonymous user for easier access
        try:
            self.authorizer.add_anonymous(str(self.user_share_dir), perm='elr')
        except Exception as e:
            self.discovery.debug_print(f"Warning: Could not add anonymous user: {e}")
        
        # Set up FTP server
        self.ftp_server = None
        self.ftp_address = ('0.0.0.0', self.config.port + 1)  # Use next port after discovery port
        # File sync settings
        self.sync_interval = 5  # Check for updates every 5 seconds
        self.sync_thread = None
        self.sync_running = False
        # Server thread
        self.server_thread = None
        self.running = False
        # Load previously shared resources
        self._load_resources()
    
    def debug_log(self, message):
        """Log debug messages to a file.
        Args:
            message: The message to log.
        """
        logger.debug(message)
    
    def _generate_password(self) -> str:
        """Generate a random password for FTP access.
        Returns:
            Random password string.
        """
        import uuid
        return str(uuid.uuid4())[:12]
    
    def _load_resources(self) -> None:
        """Load previously shared resources from disk."""
        resource_file = self.user_share_dir / '.shared_resources.json'
        if resource_file.exists():
            try:
                with open(resource_file, 'r') as f:
                    data = json.load(f)
                    for resource_data in data.get('shared', []):
                        resource = SharedResource.from_dict(resource_data)
                        self.shared_resources[resource.id] = resource
                    
                    for resource_data in data.get('received', []):
                        resource = SharedResource.from_dict(resource_data)
                        self.received_resources[resource.id] = resource
                    
                    self.downloaded_resources = set(data.get('downloaded', []))
            except Exception as e:
                self.discovery.debug_print(f"Error loading shared resources: {e}")
    
    def _save_resources(self) -> None:
        """Save shared resources to disk."""
        resource_file = self.user_share_dir / '.shared_resources.json'
        try:
            data = {
                'shared': [r.to_dict() for r in self.shared_resources.values()],
                'received': [r.to_dict() for r in self.received_resources.values()],
                'downloaded': list(self.downloaded_resources)
            }
            with open(resource_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.discovery.debug_print(f"Error saving shared resources: {e}")
    
    def start(self) -> None:
        if not self.running:
            try:
                # Create and start FTP server
                self.ftp_server = FTPServer(self.ftp_address, self.ftp_handler)
                self.server_thread = threading.Thread(target=self.ftp_server.serve_forever)
                self.server_thread.daemon = True
                self.server_thread.start()
                # Start file synchronization thread
                self.sync_running = True
                self.sync_thread = threading.Thread(target=self._file_sync_loop)
                self.sync_thread.daemon = True
                self.sync_thread.start()
                
                self.running = True
                self.discovery.debug_print(f"File sharing server started on port {self.ftp_address[1]}")
                # Announce previously shared resources
                for resource in self.shared_resources.values():
                    self._announce_resource(resource)
                    
            except Exception as e:
                self.discovery.debug_print(f"Error starting file sharing server: {e}")
    
    def stop(self) -> None:
        if self.running:
            try:
                self.running = False
                self.sync_running = False
                if self.ftp_server:
                    self.ftp_server.close_all()
                self._save_resources()
            except Exception as e:
                self.discovery.debug_print(f"Error stopping file sharing server: {e}")
    
    def _file_sync_loop(self) -> None:
        """Periodically check for file updates and synchronize them."""
        while self.sync_running:
            try:
                self._check_for_file_updates()
            except Exception as e:
                self.discovery.debug_print(f"Error in file sync loop: {e}")
            # Sleep for the sync interval
            time.sleep(self.sync_interval)
    
    def _check_for_file_updates(self) -> None:
        """Check if any shared files have been modified and update them."""
        for resource_id, resource in list(self.shared_resources.items()):
            try:
                # Check if the original file still exists
                if not os.path.exists(resource.path):
                    self.discovery.debug_print(f"Original file no longer exists: {resource.path}")
                    continue
                # Get the current modification time
                current_mod_time = os.path.getmtime(resource.path)
                # Check if the file has been modified
                if current_mod_time > resource.modified_time:
                    self.discovery.debug_print(f"File updated: {resource.path}")
                    # Update the modification time
                    resource.update_modified_time(current_mod_time)
                    # Re-copy the file to the shared directory
                    self._update_shared_copy(resource)
                    # Save the updated resource info
                    self._save_resources()
                    # Announce the update to peers
                    self._announce_resource(resource)
            except Exception as e:
                self.discovery.debug_print(f"Error checking for updates on {resource.path}: {e}")
    
    def _update_shared_copy(self, resource: SharedResource) -> None:
        """Update the shared copy of a file that has been modified.
        Args:
            resource: The shared resource to update.
        """
        try:
            # Get the filename
            filename = os.path.basename(resource.path)
            # Path in the shared directory
            share_path = self.user_share_dir / filename
            if resource.is_directory:
                # If it's a directory, remove the old one and re-copy
                if share_path.exists():
                    shutil.rmtree(share_path)
                self._recursive_copy(resource.path, share_path)
            else:
                # For files, just overwrite
                shutil.copy2(resource.path, share_path)
            self.discovery.debug_print(f"Updated shared copy of {resource.path}")
        except Exception as e:
            self.discovery.debug_print(f"Error updating shared copy: {e}")
    
    def share_resource(self, path: str, share_to_all: bool = False) -> Optional[SharedResource]:
        """Share a file or directory with peers.
        Args:
            path: Path to the file or directory to share.
            share_to_all: Whether to share with all peers.
        Returns:
            SharedResource if successful, None otherwise.
        """
        try:
            path = os.path.abspath(path)
            
            # Check if the path exists
            if not os.path.exists(path):
                self.debug_log(f"Path does not exist: {path}")
                return None
            
            # Check if trying to share the shared directory itself
            shared_dir_path = os.path.abspath(str(self.share_dir))
            if path == shared_dir_path:
                self.debug_log(f"Cannot share the 'shared' directory itself: {path}")
                return None
                
            # Check if already shared
            existing_resource = self._find_existing_shared_resource(path)
            if existing_resource:
                self.debug_log(f"Resource already shared with ID: {existing_resource.id}")
                return existing_resource
                
            is_directory = os.path.isdir(path)
            # Create shared resource with password
            resource = SharedResource(
                owner=self.username,
                path=path,
                is_directory=is_directory,
                shared_to_all=share_to_all,
                ftp_password=self.default_password
            )
            
            # Get the actual directory/file name (not '.' or '..')
            resource_name = os.path.basename(path)
            # For special cases like '.' or '..', use the actual directory name
            if resource_name in ['.', '..']:
                resource_name = os.path.basename(os.path.abspath(path))
                self.debug_log(f"Special path detected. Using actual directory name: {resource_name}")
            
            target_path = self.user_share_dir / resource_name
            
            # Handle name conflicts by appending a number
            counter = 1
            original_name = resource_name
            while target_path.exists():
                name_parts = os.path.splitext(original_name)
                resource_name = f"{name_parts[0]}_{counter}{name_parts[1] if len(name_parts) > 1 else ''}"
                target_path = self.user_share_dir / resource_name
                counter += 1
                
            # ALWAYS copy the file/directory instead of using symlinks
            # This ensures FTP access will work properly
            if is_directory:
                self._recursive_copy(path, target_path)
                self.debug_log(f"Copied directory {path} to {target_path}")
            else:
                shutil.copy2(path, target_path)
                self.debug_log(f"Copied file {path} to {target_path}")
            
            # Add to shared resources
            self.shared_resources[resource.id] = resource
            self._save_resources()
            
            # Announce to peers
            self._announce_resource(resource)
            self.debug_log(f"Shared {'directory' if is_directory else 'file'}: {path} as {resource_name}")
            return resource
        except Exception as e:
            self.debug_log(f"Error sharing resource: {e}")
            import traceback
            self.debug_log(f"Traceback: {traceback.format_exc()}")
            return None
    
    def _recursive_copy(self, src_path, dest_path):
        """Recursively copy a directory.
        Args:
            src_path: Source directory path.
            dest_path: Destination directory path.
        """
        try:
            # Convert paths to absolute paths for comparison
            abs_src_path = os.path.abspath(src_path)
            abs_dest_path = os.path.abspath(str(dest_path))
            shared_dir_path = os.path.abspath(str(self.share_dir))
            
            # Safety check: don't copy if destination is inside source (would cause infinite recursion)
            if abs_dest_path.startswith(abs_src_path):
                self.discovery.debug_print(f"Error: Destination {dest_path} is inside source {src_path}. Skipping to prevent recursion.")
                return
                
            # Safety check: don't copy if source is the shared directory itself
            if abs_src_path == shared_dir_path:
                self.discovery.debug_print(f"Error: Cannot copy from the 'shared' directory itself: {src_path}")
                return
                
            # Create destination directory
            os.makedirs(dest_path, exist_ok=True)
            
            # Copy all files and subdirectories
            for item in os.listdir(src_path):
                src_item_path = os.path.join(src_path, item)
                dest_item_path = os.path.join(dest_path, item)
                
                # Skip the shared directory if encountered
                if os.path.abspath(src_item_path) == shared_dir_path:
                    self.discovery.debug_print(f"Skipping 'shared' directory: {src_item_path}")
                    continue
                    
                if os.path.isdir(src_item_path):
                    # Recursively copy subdirectory
                    self._recursive_copy(src_item_path, dest_item_path)
                else:
                    # Copy file
                    shutil.copy2(src_item_path, dest_item_path) 
            self.discovery.debug_print(f"Recursively copied directory from {src_path} to {dest_path}")
        except Exception as e:
            self.discovery.debug_print(f"Error in recursive copy: {e}")
    
    def _announce_resource(self, resource: SharedResource) -> None:
        """Announce a shared resource to peers.
        Args:
            resource: The resource to announce.
        """
        try:
            # Update resource path if it's a special case like '.' or '..'
            original_path = resource.path
            basename = os.path.basename(original_path)
            if basename in ['.', '..']:
                # Get the actual directory name instead of '.' or '..'
                actual_dirname = os.path.basename(os.path.abspath(original_path))
                # Create a copy of the resource to avoid modifying the original
                modified_resource = SharedResource(
                    owner=resource.owner,
                    path=os.path.join(os.path.dirname(original_path), actual_dirname),
                    is_directory=resource.is_directory,
                    shared_to_all=resource.shared_to_all,
                    ftp_password=resource.ftp_password
                )
                modified_resource.id = resource.id
                modified_resource.allowed_users = resource.allowed_users.copy()
                modified_resource.timestamp = resource.timestamp
                modified_resource.modified_time = resource.modified_time
                
                # Use the modified resource for announcement
                resource_to_announce = modified_resource
                self.debug_log(f"Announcing with normalized path: {modified_resource.path} instead of {original_path}")
            else:
                resource_to_announce = resource
            
            packet = {
                'type': 'file_share',
                'action': 'announce',
                'data': resource_to_announce.to_dict()
            }
            
            # Broadcast to all peers
            self.discovery.udp_socket.sendto(
                json.dumps(packet).encode(),
                ('<broadcast>', self.config.port)
            )
            self.debug_log(f"Announced resource: {os.path.basename(resource_to_announce.path)}")
        except Exception as e:
            self.discovery.debug_print(f"Error announcing resource: {e}")
            import traceback
            self.discovery.debug_print(f"Traceback: {traceback.format_exc()}")
    
    def update_resource_access(self, resource_id: str, username: str, add: bool = True) -> bool:
        """Update access permissions for a shared resource.
        Args:
            resource_id: ID of the resource to update.
            username: Username to add or remove.
            add: True to add access, False to remove.
        Returns:
            True if successful, False otherwise.
        """
        if resource_id not in self.shared_resources:
            return False
        resource = self.shared_resources[resource_id]
        # Only the owner can modify access
        if resource.owner != self.username:
            return False
        if add:
            resource.add_user(username)
            action = 'add_access'
        else:
            resource.remove_user(username)
            action = 'remove_access'
        self._save_resources()
        # Announce access change to the specific user
        try:
            packet = {
                'type': 'file_share',
                'action': action,
                'data': {
                    'resource_id': resource_id,
                    'username': username
                }
            }
            # Send update to specific peer
            peer = self.discovery.peers.get(username)
            if peer:
                self.discovery.udp_socket.sendto(
                    json.dumps(packet).encode(),
                    (peer.address, self.config.port)
                )
                # If adding access, also re-announce the resource to trigger download
                if add:
                    self._announce_resource(resource)
                    self.discovery.debug_print(f"Re-announced resource {resource.id} after adding {username} to access list")
            
        except Exception as e:
            self.discovery.debug_print(f"Error updating resource access: {e}")
        
        return True
    
    def set_share_to_all(self, resource_id: str, share_to_all: bool) -> bool:
        """Set whether a resource is shared with all peers
        Args:
            resource_id: ID of the resource to update.
            share_to_all: Whether to share with all peers.  
        Returns:
            True if successful, False otherwise.
        """
        if resource_id not in self.shared_resources:
            return False
        resource = self.shared_resources[resource_id]
        # Only the owner can modify access
        if resource.owner != self.username:
            return False
        resource.shared_to_all = share_to_all
        self._save_resources()
        # Announce update
        self._announce_resource(resource)
        return True
    
    def handle_file_share_packet(self, packet: Dict, addr: tuple) -> None:
        """Handle a file share packet.
        
        Args:
            packet: The packet to handle.
            addr: The address the packet came from.
        """
        action = packet.get('action')
        data = packet.get('data', {})
        
        if action == 'announce':
            self._handle_resource_announcement(data, addr)
        elif action == 'add_access':
            self._handle_access_update(data, addr, add=True)
        elif action == 'remove_access':
            self._handle_access_update(data, addr, add=False)
    
    def _handle_resource_announcement(self, data: Dict, addr: tuple) -> None:
        """Handle a resource announcement.
        Args:
            data: The resource data.
            addr: The address the announcement came from.
        """
        try:
            resource = SharedResource.from_dict(data)
            # Skip if we're the owner
            if resource.owner == self.username:
                return
            # Check if we already have this resource
            existing_resource = self.received_resources.get(resource.id)
            # If we had the resource but no longer have access, remove it
            if existing_resource and not resource.can_access(self.username):
                self.discovery.debug_print(f"Access revoked for: {resource.path}")
                self._remove_shared_resource(existing_resource)
                
                # Remove from downloaded resources list
                if resource.id in self.downloaded_resources:
                    self.downloaded_resources.remove(resource.id)
                    
                # Remove from received resources
                if resource.id in self.received_resources:
                    del self.received_resources[resource.id]
                    
                self._save_resources()
                return
            # Normal handling for resources we can access
            if resource.can_access(self.username):
                if existing_resource:
                    # If we have it, check if it's been updated
                    if resource.modified_time > existing_resource.modified_time:
                        self.discovery.debug_print(f"Resource updated: {resource.path}")
                        
                        # Update the resource in our records
                        self.received_resources[resource.id] = resource
                        self._save_resources()
                        
                        # Remove from downloaded list to force re-download
                        if resource.id in self.downloaded_resources:
                            self.downloaded_resources.remove(resource.id)
                        
                        # Download the updated resource
                        download_thread = threading.Thread(
                            target=self._download_resource,
                            args=(resource, addr[0])
                        )
                        download_thread.daemon = True
                        download_thread.start()
                        
                        self.discovery.debug_print(f"Downloading updated resource: {resource.path}")
                else:
                    # New resource, store it
                    self.received_resources[resource.id] = resource
                    self._save_resources()
                    
                    # Create the owner's directory if it doesn't exist
                    owner_dir = self.share_dir / resource.owner
                    owner_dir.mkdir(exist_ok=True)
                    
                    # Download the resource if we haven't already
                    if resource.id not in self.downloaded_resources:
                        download_thread = threading.Thread(
                            target=self._download_resource,
                            args=(resource, addr[0])
                        )
                        download_thread.daemon = True
                        download_thread.start()
                        
                        self.discovery.debug_print(f"Received new shared resource: {resource.path}")
            
        except Exception as e:
            self.discovery.debug_print(f"Error handling resource announcement: {e}")
        

    def _download_resource(self, resource: SharedResource, host_ip: str) -> None:
        """Download a resource from a peer.
        Args:
            resource: The resource to download.
            host_ip: The IP address of the host.
        """
        try:
            resource_name = os.path.basename(resource.path)
            self.debug_log(f"Downloading {resource_name} from {host_ip}...")
            
            # Create destination path
            dest_dir = self.share_dir / resource.owner
            dest_path = dest_dir / resource_name
            
            # If the file already exists and this is an update, remove the old version
            if dest_path.exists() and resource.id in self.received_resources:
                if resource.is_directory:
                    shutil.rmtree(dest_path)
                else:
                    os.remove(dest_path)
                
                self.debug_log(f"Removed old version of {dest_path}")
            
            # Create FTP connection
            ftp = ftplib.FTP()
            ftp.connect(host_ip, self.ftp_address[1])
            
            # Keep encoding as UTF-8 for command channel
            ftp.encoding = 'utf-8'
            
            # Try different login methods
            login_successful = False
            
            # First, try using the provided password
            if resource.ftp_password:
                try:
                    ftp.login(resource.owner, resource.ftp_password)
                    login_successful = True
                    self.debug_log(f"Logged in with username and password")
                except Exception as e:
                    self.debug_log(f"FTP login with owner credentials failed: {e}")
            
            # If that fails, try anonymous login
            if not login_successful:
                try:
                    ftp.login('anonymous', 'anonymous@')
                    login_successful = True
                    self.debug_log(f"Logged in anonymously")
                except Exception as e:
                    self.debug_log(f"Anonymous FTP login failed: {e}")
            
            # If all login attempts failed, we can't proceed
            if not login_successful:
                self.debug_log(f"All FTP login attempts failed - cannot download resource")
                return
            
            # Explicitly set binary mode for file transfers
            ftp.sendcmd('TYPE I')
            
            # First, get a complete listing of the FTP directory to know what's actually there
            try:
                # Get list of files and directories in the root directory
                ftp_files = []
                ftp.dir(ftp_files.append)
                self.debug_log(f"FTP directory contents: {ftp_files}")
                
                # Parse directory listing to get actual available items
                available_items = []
                for item in ftp_files:
                    if not item.strip():
                        continue
                        
                    # Try to extract the filename (always at the end of the listing)
                    parts = item.split()
                    if len(parts) > 0:
                        filename = parts[-1]
                        is_dir = item.strip().startswith('d')
                        available_items.append((filename, is_dir))
                
                self.debug_log(f"Available items on FTP server: {available_items}")
                
                # Check if our resource exists in the available items
                found_item = None
                for filename, is_dir in available_items:
                    if filename == resource_name:
                        found_item = (filename, is_dir)
                        break
                
                if not found_item:
                    # Resource not found by exact name, let's see if it's a special case like a directory
                    # For directories, the original name might be different due to normalization
                    self.debug_log(f"Resource {resource_name} not found by exact name, checking alternatives")
                    
                    # Check if any of the available items matches our resource ID
                    resource_id_parts = resource.id.split('_')
                    if len(resource_id_parts) >= 3:
                        orig_resource_name = resource_id_parts[2]  # The part after owner_timestamp_
                        self.debug_log(f"Checking for original resource name from ID: {orig_resource_name}")
                        
                        for filename, is_dir in available_items:
                            if filename == orig_resource_name:
                                found_item = (filename, is_dir)
                                resource_name = filename  # Update resource_name to match what's on the server
                                self.debug_log(f"Found resource by ID-derived name: {filename}")
                                break
                
                if not found_item:
                    self.debug_log(f"Resource not found on FTP server: checked {resource_name}")
                    ftp.quit()
                    return
                    
                filename, is_dir = found_item
                self.debug_log(f"Found resource on FTP server: {filename} (is_directory: {is_dir})")
                
                # Now proceed with download based on what we found
                if is_dir:
                    # Directory download
                    os.makedirs(dest_path, exist_ok=True)
                    self.debug_log(f"Downloading directory: {filename}")
                    try:
                        self._download_directory_recursive(ftp, filename, dest_path)
                        self.downloaded_resources.add(resource.id)
                        self._save_resources()
                        self.debug_log(f"Downloaded directory {resource.path} to {dest_path}")
                    except Exception as e:
                        self.debug_log(f"Error downloading directory: {e}")
                        import traceback
                        self.debug_log(f"Traceback: {traceback.format_exc()}")
                else:
                    # File download
                    os.makedirs(dest_dir, exist_ok=True)
                    self.debug_log(f"Downloading file: {filename}")
                    
                    # Open file in binary write mode
                    with open(dest_path, 'wb') as f:
                        def callback(data):
                            f.write(data)
                        
                        # Use binary transfer mode with optimized block size
                        self.debug_log(f"Using RETR command with block size 8192")
                        ftp.retrbinary(f'RETR {filename}', callback, blocksize=8192)
                    
                    # Verify successful download
                    file_size = os.path.getsize(dest_path)
                    self.debug_log(f"Download completed. File size: {file_size} bytes")
                    
                    if file_size > 0:
                        self.downloaded_resources.add(resource.id)
                        self._save_resources()
                        self.debug_log(f"Successfully downloaded file to {dest_path} ({file_size} bytes)")
                    else:
                        self.debug_log(f"Warning: Downloaded file is empty")
                        os.remove(dest_path)
                        
            except Exception as e:
                self.debug_log(f"Error accessing FTP server: {e}")
                import traceback
                self.debug_log(f"Traceback: {traceback.format_exc()}")
            
            # Close connection
            ftp.quit()
            
        except Exception as e:
            self.debug_log(f"Error in download_resource: {e}")
            import traceback
            self.debug_log(f"Traceback: {traceback.format_exc()}")
            
    def _download_directory_recursive(self, ftp, remote_dir, local_dir):
        """Download a directory recursively.
        Args:
            ftp: FTP connection.
            remote_dir: Remote directory name.
            local_dir: Local directory path.
        """
        try:
            # Create local directory
            os.makedirs(local_dir, exist_ok=True)
            
            # Remember current directory
            original_dir = ftp.pwd()
            
            try:
                # Change to remote directory
                ftp.cwd(remote_dir)
                self.debug_log(f"Changed to directory: {remote_dir}")
                
                # Get directory listing
                file_list = []
                ftp.dir(file_list.append)
                self.debug_log(f"Directory contents: {file_list}")
                
                # Process each item
                for item in file_list:
                    # Skip empty items
                    if not item.strip():
                        continue
                        
                    # Parse the directory listing line
                    # Format is typically: "drwxr-xr-x   2 user  group    4096 Jan 01 12:34 filename"
                    parts = item.split(None, 8)
                    
                    # If not enough parts, try a simpler approach
                    if len(parts) < 9:
                        self.debug_log(f"Complex parsing failed for {item}, trying simpler approach")
                        # Just get the last part of the listing as the name
                        # This is less accurate but better than skipping
                        simple_parts = item.split()
                        if not simple_parts:
                            self.debug_log(f"Skipping invalid listing item: {item}")
                            continue
                        name = simple_parts[-1]
                        # Guess if it's a directory by 'd' at the start of the line
                        is_dir = item.strip().startswith('d')
                    else:
                        # Standard case - extract name from the parsed parts
                        name = parts[8]
                        is_dir = parts[0].startswith('d')
                    
                    # Skip special directories
                    if name in ('.', '..'):
                        continue
                        
                    # Create full local path for this item
                    local_item_path = os.path.join(local_dir, name)
                    
                    if is_dir:
                        # Recursively download directory
                        self.debug_log(f"Found subdirectory: {name}")
                        self._download_directory_recursive(ftp, name, local_item_path)
                    else:
                        # Download file in binary mode
                        self.debug_log(f"Downloading file: {name} to {local_item_path}")
                        
                        try:
                            # Open in binary mode with simpler callback
                            with open(local_item_path, 'wb') as f:
                                def callback(data):
                                    f.write(data)
                                
                                # Use more reliable block size
                                self.debug_log(f"Using RETR command with block size 8192")
                                ftp.retrbinary(f'RETR {name}', callback, blocksize=8192)
                            
                            # Verify file was downloaded successfully
                            file_size = os.path.getsize(local_item_path)
                            self.debug_log(f"First download attempt completed. File size: {file_size} bytes")
                            
                            if file_size == 0:
                                self.debug_log(f"Warning: Downloaded file {local_item_path} is empty! Trying again...")
                                # Try one more time with smaller block size
                                with open(local_item_path, 'wb') as f:
                                    self.debug_log(f"Using RETR command with block size 1024")
                                    ftp.retrbinary(f'RETR {name}', f.write, blocksize=1024)
                                
                                # Check again
                                file_size = os.path.getsize(local_item_path)
                                if file_size == 0:
                                    self.debug_log(f"Second attempt failed. File still empty.")
                                    # Delete empty file
                                    os.remove(local_item_path)
                                    self.debug_log(f"Removed empty file: {local_item_path}")
                                else:
                                    self.debug_log(f"Second attempt successful. File size: {file_size} bytes")
                            else:
                                self.debug_log(f"Successfully downloaded file {name} ({file_size} bytes)")
                        except Exception as e:
                            self.debug_log(f"Error downloading file {name}: {e}")
                            # Try to continue with other files even if one fails
                
                # Return to original directory after processing all items
                ftp.cwd(original_dir)
                
            except Exception as e:
                self.debug_log(f"Error during directory download: {e}")
                import traceback
                self.debug_log(f"Traceback: {traceback.format_exc()}")
                # Try to go back to original directory
                try:
                    ftp.cwd(original_dir)
                except:
                    pass
                raise
                    
        except Exception as e:
            self.debug_log(f"Error downloading directory {remote_dir}: {e}")
            import traceback
            self.debug_log(f"Traceback: {traceback.format_exc()}")
            
    def _handle_access_update(self, data: Dict, addr: tuple, add: bool) -> None:
        """Handle an access update.
        Args:
            data: The update data.
            addr: The address the update came from.
            add: True if adding access, False if removing.
        """
        try:
            resource_id = data.get('resource_id')
            username = data.get('username')
            if username != self.username:
                return
            # Check if we have this resource
            for resources in [self.shared_resources, self.received_resources]:
                if resource_id in resources:
                    resource = resources[resource_id]
                    if add:
                        resource.add_user(username)
                        # Download the resource if we're being granted access
                        if resource.owner != self.username and resource_id not in self.downloaded_resources:
                            peer = self.discovery.peers.get(resource.owner)
                            if peer:
                                # Make sure the owner's directory exists
                                owner_dir = self.share_dir / resource.owner
                                owner_dir.mkdir(exist_ok=True)
                                
                                download_thread = threading.Thread(
                                    target=self._download_resource,
                                    args=(resource, peer.address)
                                )
                                download_thread.daemon = True
                                download_thread.start()
                                
                                self.discovery.debug_print(f"Starting download for newly granted access to {resource.path}")
                    else:
                        # Remove access
                        resource.remove_user(username)
                        # If we're receiving this message and we're not the owner, remove the file
                        if resource.owner != self.username:
                            self._remove_shared_resource(resource)
                            # Also remove from downloaded resources list
                            if resource_id in self.downloaded_resources:
                                self.downloaded_resources.remove(resource_id)
                                
                            # Remove from received resources if it's not shared to all
                            if not resource.shared_to_all and resource_id in self.received_resources:
                                del self.received_resources[resource_id]
                    self._save_resources()
                    action_str = "added to" if add else "removed from"
                    self.discovery.debug_print(
                        f"You were {action_str} the access list for {os.path.basename(resource.path)} from {resource.owner}"
                    )
        
        except Exception as e:
            self.discovery.debug_print(f"Error handling access update: {e}")
            
    def _remove_shared_resource(self, resource: SharedResource) -> None:
        try:
            # Get the path to the resource in the shared directory
            resource_path = self.share_dir / resource.owner / os.path.basename(resource.path)
            if not resource_path.exists():
                self.discovery.debug_print(f"Resource not found for removal: {resource_path}")
                return
            # Remove the file or directory
            if resource.is_directory:
                try:
                    shutil.rmtree(resource_path)
                    self.discovery.debug_print(f"Removed shared directory: {resource_path}")
                except Exception as e:
                    self.discovery.debug_print(f"Error removing directory {resource_path}: {e}")
            else:
                try:
                    os.remove(resource_path)
                    self.discovery.debug_print(f"Removed shared file: {resource_path}")
                except Exception as e:
                    self.discovery.debug_print(f"Error removing file {resource_path}: {e}")
        except Exception as e:
            self.discovery.debug_print(f"Error removing shared resource: {e}")
    
    def _find_existing_shared_resource(self, path: str) -> Optional[SharedResource]:
        normalized_path = os.path.abspath(path)
        # Check if the same path is already shared
        for resource in self.shared_resources.values():
            if os.path.abspath(resource.path) == normalized_path and resource.owner == self.username:
                return resource
        return None
    
    def list_shared_resources(self, include_own: bool = True) -> List[SharedResource]:
        resources = []
        if include_own:
            resources.extend(self.shared_resources.values())
        resources.extend(self.received_resources.values())
        return sorted(resources, key=lambda r: r.timestamp, reverse=True)
    
    def get_resource_by_id(self, resource_id: str) -> Optional[SharedResource]:
        if resource_id in self.shared_resources:
            return self.shared_resources[resource_id]
        if resource_id in self.received_resources:
            return self.received_resources[resource_id]
        return None