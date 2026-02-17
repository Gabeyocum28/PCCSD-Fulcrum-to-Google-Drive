"""
Fulcrum to Google Drive Direct Export
Exports forms directly from Fulcrum API to Google Drive without local storage.
"""

import os
import io
import csv
import json
import pickle
import logging
import requests
import ssl
import time
import threading
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Set
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from googleapiclient.errors import HttpError
from dotenv import load_dotenv
from tqdm import tqdm
import atexit
import signal

# Load environment variables
load_dotenv()

# Timeout configuration (in seconds)
# GitHub Actions has 6-hour limit; we save state at 5h45m to allow graceful exit
WORKFLOW_TIMEOUT_SECONDS = 5 * 60 * 60 + 45 * 60  # 5 hours 45 minutes

# Utah timezone (Mountain Time)
UTAH_TZ = ZoneInfo('America/Denver')

# Custom formatter for Utah time
class UtahTimeFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=UTAH_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime('%Y-%m-%d %H:%M:%S')

# Configure logging with Utah timezone
# Force immediate flushing for GitHub Actions compatibility
import sys

class FlushingHandler(logging.StreamHandler):
    def emit(self, record):
        super().emit(record)
        self.flush()

formatter = UtahTimeFormatter('%(asctime)s - %(levelname)s - %(message)s')

handler_file = logging.FileHandler('fulcrum_to_google_drive.log')
handler_file.setFormatter(formatter)

handler_console = FlushingHandler(sys.stdout)
handler_console.setFormatter(formatter)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(handler_file)
logger.addHandler(handler_console)
logger.propagate = False  # Prevent duplicate output from root logger

# Suppress file_cache warnings from Google API library
logging.getLogger('googleapiclient.discovery_cache').setLevel(logging.ERROR)

SCOPES = ['https://www.googleapis.com/auth/drive']
SCRIPT_DIR = Path(__file__).parent


class FulcrumToDriveExporter:
    """Export Fulcrum data directly to Google Drive"""

    def __init__(self, fulcrum_token: str, drive_folder_name: str = "Fulcrum-Auto Update/Initial Sync", pre_approved_forms: List[str] = None, skip_deletions: bool = False, auto_delete: bool = False, quick_check: bool = False):
        self.fulcrum_token = fulcrum_token
        self.drive_folder_name = drive_folder_name
        self.skip_deletions = skip_deletions  # Auto-skip all deletion requests
        self.auto_delete = auto_delete  # Auto-delete all orphaned photos without approval
        self.quick_check = quick_check  # Skip entire form if folder exists
        self.fulcrum_base_url = "https://api.fulcrumapp.com/api/v2"
        self.fulcrum_headers = {
            "X-ApiToken": fulcrum_token,
            "Accept": "application/json"
        }

        # Google Drive setup
        self.drive_service = None
        self.drive_creds = None  # Store credentials for thread-local services
        self._thread_local = threading.local()  # Thread-local storage for services
        self.drive_folder_id = None
        self.active_forms_id = None
        self.inactive_forms_id = None
        self._folder_cache = {}  # Cache folder IDs to avoid repeated lookups
        self._contents_cache = {}  # Cache folder contents to avoid repeated listings
        self._new_folders = set()  # Track newly created folders (known to be empty)

        # Stats
        self.stats = {
            "forms_processed": 0,
            "photos_uploaded": 0,
            "photos_skipped": 0,
            "photos_failed": 0,
            "photos_deleted": 0,
            "geojson_uploaded": 0,
            "total_records": 0,
            "layers_uploaded": 0
        }

        # Track forms with failed photos for summary
        self.failed_forms = []

        # Slack integration for deletion approval
        self.slack_bot_token = os.getenv('SLACK_BOT_TOKEN')
        self.slack_channel_id = os.getenv('SLACK_CHANNEL_ID')
        self.slack_enabled = bool(self.slack_bot_token and self.slack_channel_id)
        self._export_cancelled = False  # Flag to stop export when user sends 'e' or 'end'
        self._skipped_forms = []  # Track forms skipped due to timeout or user request
        self._pre_approved_forms = set(pre_approved_forms) if pre_approved_forms else set()
        self._skipped_forms_file = SCRIPT_DIR / 'skipped_forms.json'  # Persist skipped forms for re-runs

        # Progress tracking for resume functionality
        self._progress_file = SCRIPT_DIR / 'export_progress.json'
        self._uploaded_photos = set()  # Track photos uploaded in this session + previous
        self._export_start_time = None  # Track when export started for timeout
        self._timeout_seconds = WORKFLOW_TIMEOUT_SECONDS
        self._state_file = SCRIPT_DIR / 'export_state.json'  # For workflow chaining
        self._current_form_index = 0  # Track progress for state saving

    def send_slack_message(self, text: str) -> str:
        """Send a message to Slack channel, returns message timestamp or None on failure"""
        if not self.slack_enabled:
            return None

        try:
            response = requests.post(
                'https://slack.com/api/chat.postMessage',
                headers={
                    'Authorization': f'Bearer {self.slack_bot_token}',
                    'Content-Type': 'application/json'
                },
                json={
                    'channel': self.slack_channel_id,
                    'text': text
                },
                timeout=30
            )
            data = response.json()
            if data.get('ok'):
                return data.get('ts')  # Message timestamp
            else:
                logger.warning(f"Slack message failed: {data.get('error')}")
                return None
        except Exception as e:
            logger.warning(f"Failed to send Slack message: {e}")
            return None

    def get_slack_messages_since(self, oldest_ts: str) -> List[Dict]:
        """Get messages from Slack channel since a timestamp"""
        if not self.slack_enabled:
            return []

        try:
            response = requests.get(
                'https://slack.com/api/conversations.history',
                headers={
                    'Authorization': f'Bearer {self.slack_bot_token}',
                },
                params={
                    'channel': self.slack_channel_id,
                    'oldest': oldest_ts,
                    'limit': 100
                },
                timeout=30
            )
            data = response.json()
            if data.get('ok'):
                return data.get('messages', [])
            else:
                logger.warning(f"Slack history failed: {data.get('error')}")
                return []
        except Exception as e:
            logger.warning(f"Failed to get Slack messages: {e}")
            return []

    def wait_for_slack_approval(self, message_ts: str, timeout_minutes: int = 5) -> str:
        """Wait for user response in Slack after a message (5 min default timeout)
        Returns: 'approve', 'skip', 'end', or 'timeout'"""
        if not self.slack_enabled:
            return 'approve'  # Auto-approve if Slack not configured

        start_time = time.time()
        timeout_seconds = timeout_minutes * 60
        poll_interval = 5  # Check every 5 seconds

        logger.debug("  Waiting for Slack approval...")

        while time.time() - start_time < timeout_seconds:
            messages = self.get_slack_messages_since(message_ts)

            for msg in messages:
                # Skip bot messages and the original message
                if msg.get('bot_id') or msg.get('ts') == message_ts:
                    continue

                text = msg.get('text', '').strip().lower()

                # Check for approval
                if text in ['y', 'yes']:
                    return 'approve'

                # Check for skip
                if text in ['s', 'skip']:
                    return 'skip'

                # Check for end
                if text in ['e', 'end']:
                    return 'end'

            time.sleep(poll_interval)

        return 'timeout'

    def identify_orphaned_photos(self, photos_folder_id: str, photo_metadata_cache: Dict[str, Dict]) -> List[str]:
        """Identify photos in Drive that were deleted in Fulcrum
        Returns list of filenames to delete"""
        existing_in_drive = self._list_drive_folder_contents(photos_folder_id)

        if not existing_in_drive:
            return []

        valid_photo_ids = set(photo_metadata_cache.keys())
        orphaned = []

        for filename in existing_in_drive:
            if '.' in filename:
                photo_id = filename.rsplit('.', 1)[0]
            else:
                continue

            if photo_id not in valid_photo_ids:
                orphaned.append(filename)

        return orphaned

    def delete_photos_from_drive(self, filenames: List[str], photos_folder_id: str) -> int:
        """Delete specific photos from Drive folder, returns count deleted.
        Lists folder once to get file IDs, then deletes by ID (avoids N list queries)."""
        filenames_to_delete = set(filenames)

        # Single list call to get all file IDs in the folder
        file_id_map = {}
        page_token = None
        while True:
            results = self.drive_service.files().list(
                q=f"'{photos_folder_id}' in parents and trashed=false",
                spaces='drive',
                fields='nextPageToken, files(id, name)',
                pageSize=1000,
                pageToken=page_token
            ).execute()
            for f in results.get('files', []):
                if f['name'] in filenames_to_delete:
                    file_id_map[f['name']] = f['id']
            page_token = results.get('nextPageToken')
            if not page_token:
                break

        # Delete by ID (1 API call per file instead of 2)
        deleted = 0
        for filename, file_id in file_id_map.items():
            try:
                self.drive_service.files().delete(fileId=file_id).execute()
                deleted += 1
            except HttpError as e:
                logger.debug(f"Error deleting {filename}: {e}")

        # Invalidate contents cache since we modified the folder
        self._contents_cache.pop(photos_folder_id, None)
        return deleted

    def init_google_drive(self):
        """Initialize Google Drive connection"""
        creds = None
        token_path = SCRIPT_DIR / 'token.pickle'
        credentials_path = SCRIPT_DIR / 'credentials.json'

        if token_path.exists():
            with open(token_path, 'rb') as token:
                creds = pickle.load(token)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not credentials_path.exists():
                    logger.error("credentials.json not found!")
                    return False
                flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
                creds = flow.run_local_server(port=0)

            with open(token_path, 'wb') as token:
                pickle.dump(creds, token)

        self.drive_creds = creds  # Store for thread-local services
        self._token_path = token_path  # Store for refresh
        self.drive_service = build('drive', 'v3', credentials=creds)
        self._last_token_refresh = time.time()

        # Find the target folder (supports nested paths like "Parent/Child/Grandchild")
        folder_path = self.drive_folder_name.split('/')
        current_folder_id = None

        for i, folder_name in enumerate(folder_path):
            folder_name = folder_name.strip()
            if not folder_name:
                continue

            if current_folder_id is None:
                # First folder - search from root
                query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
            else:
                # Subfolder - search within parent
                query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and '{current_folder_id}' in parents and trashed=false"

            results = self.drive_service.files().list(
                q=query,
                spaces='drive',
                fields='files(id, name)'
            ).execute()

            folders = results.get('files', [])
            if not folders:
                path_so_far = '/'.join(folder_path[:i+1])
                logger.error(f"Folder '{folder_name}' not found in path '{path_so_far}'")
                return False

            current_folder_id = folders[0]['id']

        self.drive_folder_id = current_folder_id
        logger.debug(f"Connected to Google Drive folder: {self.drive_folder_name}")

        # Get or create active_forms and inactive_forms subfolders
        self.active_forms_id = self._get_or_create_folder("active_forms", self.drive_folder_id)
        self.inactive_forms_id = self._get_or_create_folder("inactive_forms", self.drive_folder_id)

        return True

    def _refresh_drive_token_if_needed(self, force: bool = False):
        """Refresh Google Drive token if it's been more than 45 minutes (or force refresh)"""
        elapsed = time.time() - self._last_token_refresh
        if force or elapsed > 30 * 60:  # 30 minutes (tokens expire after 1 hour)
            try:
                logger.debug("Refreshing Google Drive token...")
                # Force refresh the credentials
                self.drive_creds.refresh(Request())
                # Save refreshed token
                with open(self._token_path, 'wb') as token:
                    pickle.dump(self.drive_creds, token)
                # Rebuild service with refreshed creds
                self.drive_service = build('drive', 'v3', credentials=self.drive_creds)
                # Clear thread-local services so they rebuild with new creds
                self._thread_local = threading.local()
                self._last_token_refresh = time.time()
                return True
            except Exception as e:
                logger.warning(f"Token refresh failed: {e}")
                return False
        return True

    def _get_thread_service(self):
        """Get a thread-local Drive service for safe concurrent uploads"""
        if not hasattr(self._thread_local, 'service') or self._thread_local.service is None:
            self._thread_local.service = build('drive', 'v3', credentials=self.drive_creds)
        return self._thread_local.service

    def _get_or_create_folder(self, name: str, parent_id: str) -> str:
        """Get existing folder or create new one in Drive (with caching)"""
        cache_key = f"{parent_id}/{name}"
        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]

        query = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and '{parent_id}' in parents and trashed=false"
        results = self.drive_service.files().list(q=query, spaces='drive', fields='files(id)').execute()
        folders = results.get('files', [])

        if folders:
            self._folder_cache[cache_key] = folders[0]['id']
            return folders[0]['id']

        metadata = {
            'name': name,
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [parent_id]
        }
        folder = self.drive_service.files().create(body=metadata, fields='id').execute()
        folder_id = folder.get('id')
        self._folder_cache[cache_key] = folder_id
        self._new_folders.add(folder_id)  # Track as newly created (empty)
        return folder_id

    def _list_drive_folder_contents(self, folder_id: str, use_cache: bool = True) -> Set[str]:
        """List all file names in a Drive folder (with caching)"""
        # Check cache first
        if use_cache and folder_id in self._contents_cache:
            return self._contents_cache[folder_id]

        # Check if this is a newly created folder (empty)
        if folder_id in self._new_folders:
            self._contents_cache[folder_id] = set()
            return set()

        all_files = set()
        page_token = None

        while True:
            results = self.drive_service.files().list(
                q=f"'{folder_id}' in parents and trashed=false",
                spaces='drive',
                fields='nextPageToken, files(name)',
                pageSize=1000,
                pageToken=page_token
            ).execute()

            for f in results.get('files', []):
                all_files.add(f['name'])

            page_token = results.get('nextPageToken')
            if not page_token:
                break

        # Cache the results
        if use_cache:
            self._contents_cache[folder_id] = all_files

        return all_files

    def _preload_existing_folders(self):
        """Preload all existing form folders to minimize API calls.
        Only caches form-level folders; subfolders (photos, geojson) are looked up on demand."""
        logger.info("Pre-loading existing folder structure...")

        # List all folders in active_forms and inactive_forms
        for parent_name, parent_id in [("active_forms", self.active_forms_id), ("inactive_forms", self.inactive_forms_id)]:
            if not parent_id:
                continue

            # Get all form folders
            form_count = 0
            page_token = None
            while True:
                results = self.drive_service.files().list(
                    q=f"'{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
                    spaces='drive',
                    fields='nextPageToken, files(id, name)',
                    pageSize=1000,
                    pageToken=page_token
                ).execute()

                for f in results.get('files', []):
                    form_count += 1
                    cache_key = f"{parent_id}/{f['name']}"
                    self._folder_cache[cache_key] = f['id']

                page_token = results.get('nextPageToken')
                if not page_token:
                    break

            logger.info(f"  Found {form_count} existing {parent_name} folders")

        logger.info(f"  Cached {len(self._folder_cache)} folder IDs")

    def _load_photo_progress(self):
        """Load previously uploaded photos from progress file"""
        if self._progress_file.exists():
            try:
                with open(self._progress_file, 'r') as f:
                    data = json.load(f)
                    self._uploaded_photos = set(data.get('uploaded_photos', []))
                    logger.info(f"Loaded progress: {len(self._uploaded_photos)} photos already uploaded")
            except Exception as e:
                logger.warning(f"Failed to load progress file: {e}")
                self._uploaded_photos = set()

    def _save_photo_progress(self):
        """Save uploaded photos to progress file for resume"""
        try:
            with open(self._progress_file, 'w') as f:
                json.dump({
                    'uploaded_photos': list(self._uploaded_photos),
                    'last_updated': datetime.now(UTAH_TZ).isoformat()
                }, f)
        except Exception as e:
            logger.warning(f"Failed to save progress: {e}")

    def _record_photo_uploaded(self, photo_id: str):
        """Record a successfully uploaded photo"""
        self._uploaded_photos.add(photo_id)
        # Save progress periodically (every 100 photos to avoid excessive writes)
        if len(self._uploaded_photos) % 100 == 0:
            self._save_photo_progress()

    def _is_photo_uploaded(self, photo_id: str) -> bool:
        """Check if a photo was already uploaded in a previous run"""
        return photo_id in self._uploaded_photos

    def _check_timeout(self) -> bool:
        """Check if we're approaching the workflow timeout"""
        if self._export_start_time is None:
            return False
        elapsed = time.time() - self._export_start_time
        return elapsed >= self._timeout_seconds

    def _save_export_state(self, form_index: int, forms: List[Dict]):
        """Save export state for workflow chaining continuation"""
        try:
            state = {
                'form_index': form_index,
                'total_forms': len(forms),
                'stats': self.stats,
                'timestamp': datetime.now(UTAH_TZ).isoformat(),
                'needs_continuation': True
            }
            with open(self._state_file, 'w') as f:
                json.dump(state, f, indent=2)
            # Also save photo progress
            self._save_photo_progress()
            logger.info(f"Saved export state at form {form_index}/{len(forms)} for continuation")
        except Exception as e:
            logger.error(f"Failed to save export state: {e}")

    def _load_export_state(self) -> Dict:
        """Load previous export state if exists"""
        if self._state_file.exists():
            try:
                with open(self._state_file, 'r') as f:
                    state = json.load(f)
                    if state.get('needs_continuation'):
                        logger.info(f"Found continuation state: form {state.get('form_index')}/{state.get('total_forms')}")
                        return state
            except Exception as e:
                logger.warning(f"Failed to load export state: {e}")
        return None

    def _clear_export_state(self):
        """Clear export state after successful completion"""
        try:
            if self._state_file.exists():
                self._state_file.unlink()
            if self._progress_file.exists():
                self._progress_file.unlink()
            logger.debug("Cleared export state files")
        except Exception as e:
            logger.warning(f"Failed to clear state files: {e}")

    def _clear_folder_contents(self, folder_id: str) -> int:
        """Delete all files and subfolders inside a Drive folder. Returns count deleted."""
        # List all items first, then delete (avoids pagination shift during deletion)
        all_items = []
        page_token = None
        while True:
            results = self.drive_service.files().list(
                q=f"'{folder_id}' in parents and trashed=false",
                spaces='drive',
                fields='nextPageToken, files(id, name, mimeType)',
                pageSize=1000,
                pageToken=page_token
            ).execute()
            all_items.extend(results.get('files', []))
            page_token = results.get('nextPageToken')
            if not page_token:
                break

        deleted = 0
        for f in all_items:
            try:
                if f['mimeType'] == 'application/vnd.google-apps.folder':
                    deleted += self._clear_folder_contents(f['id'])
                self.drive_service.files().delete(fileId=f['id']).execute()
                deleted += 1
            except HttpError as e:
                logger.debug(f"Error deleting {f['name']}: {e}")

        # Invalidate cache for this folder
        self._contents_cache.pop(folder_id, None)
        return deleted

    def _delete_file_if_exists(self, filename: str, parent_id: str) -> bool:
        """Delete a file by name in a folder if it exists"""
        try:
            query = f"name='{filename}' and '{parent_id}' in parents and trashed=false"
            results = self.drive_service.files().list(q=query, spaces='drive', fields='files(id)').execute()
            files = results.get('files', [])
            for f in files:
                self.drive_service.files().delete(fileId=f['id']).execute()
            return True
        except HttpError as e:
            logger.debug(f"Error deleting {filename}: {e}")
            return False

    def cleanup_deleted_photos(self, photos_folder_id: str, photo_metadata_cache: Dict[str, Dict], form_name: str = "Unknown") -> tuple:
        """Remove photos from Drive that were deleted in Fulcrum
        With Slack enabled, sends notification and waits for approval.
        Returns: (count_deleted, action) where action is 'approved', 'skipped', or 'ended'"""
        # Identify orphaned photos
        orphaned_files = self.identify_orphaned_photos(photos_folder_id, photo_metadata_cache)

        if not orphaned_files:
            return 0, 'approved'

        # Auto-skip all deletions if flag is set
        if self.skip_deletions:
            logger.debug(f"  Skipped deletion of {len(orphaned_files)} orphaned photos")
            return 0, 'skipped'

        # Auto-delete all orphaned photos without approval
        if self.auto_delete:
            logger.info(f"  Auto-deleting {len(orphaned_files)} orphaned photos from {form_name}")
            if self.slack_enabled:
                self.send_slack_message(f"🗑️ *Auto-delete:* Removing {len(orphaned_files)} orphaned photos from *{form_name}*")
            deleted_count = self.delete_photos_from_drive(orphaned_files, photos_folder_id)
            return deleted_count, 'approved'

        # Check if this form is pre-approved (auto-approve without Slack wait)
        if form_name in self._pre_approved_forms:
            if self.slack_enabled:
                self.send_slack_message(f"✅ *Pre-approved:* Deleting {len(orphaned_files)} photos from *{form_name}*")
            deleted_count = self.delete_photos_from_drive(orphaned_files, photos_folder_id)
            return deleted_count, 'approved'

        # If Slack is enabled, request approval
        if self.slack_enabled:
            message = (
                f"🗑️ *Photo Deletion Approval Required*\n\n"
                f"*Form:* {form_name}\n"
                f"*Photos to delete:* {len(orphaned_files)}\n\n"
                f"These photos were deleted in Fulcrum and need to be removed from Google Drive.\n\n"
                f"Reply within 5 minutes:\n"
                f"• `y` or `yes` - Approve deletion and continue\n"
                f"• `s` or `skip` - Skip this form (keep photos) and continue\n"
                f"• `e` or `end` - End the export process"
            )

            msg_ts = self.send_slack_message(message)
            if not msg_ts:
                logger.warning("  Failed to send Slack notification, auto-approving deletion")
            else:
                response = self.wait_for_slack_approval(msg_ts)

                if response == 'skip':
                    self._skipped_forms.append({"form": form_name, "reason": "user requested", "photos": len(orphaned_files)})
                    self.send_slack_message(f"✓ Skipped deletion for *{form_name}*")
                    return 0, 'skipped'

                if response == 'end':
                    self.send_slack_message("🛑 Export process ended by user request")
                    self._export_cancelled = True
                    return 0, 'ended'

                if response == 'timeout':
                    self._skipped_forms.append({"form": form_name, "reason": "no response", "photos": len(orphaned_files)})
                    self.send_slack_message(f"⏰ Timeout - skipped deletion for *{form_name}* ({len(orphaned_files)} photos)")
                    return 0, 'skipped'

                # response == 'approve'
                self.send_slack_message(f"✓ Deleting {len(orphaned_files)} photos from *{form_name}*...")

        # Proceed with deletion
        deleted_count = self.delete_photos_from_drive(orphaned_files, photos_folder_id)
        return deleted_count, 'approved'

    def _upload_to_drive(self, content: bytes, filename: str, parent_id: str, mime_type: str, max_retries: int = 3, use_thread_service: bool = False) -> bool:
        """Upload content directly to Google Drive with retry logic"""
        # Use thread-local service for concurrent uploads, main service otherwise
        service = self._get_thread_service() if use_thread_service else self.drive_service

        # Use simple upload for files < 5MB (faster, no extra round-trips)
        # Use resumable upload for larger files (handles interruptions)
        use_resumable = len(content) >= 5 * 1024 * 1024  # 5MB threshold

        for attempt in range(max_retries):
            try:
                # Create new file (skip existence check for speed)
                metadata = {'name': filename, 'parents': [parent_id]}
                media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type, resumable=use_resumable)

                service.files().create(
                    body=metadata,
                    media_body=media,
                    fields='id'
                ).execute()
                return True

            except HttpError as e:
                if e.resp.status in [429, 403, 500, 503]:
                    # Rate limit or server error - exponential backoff
                    wait_time = (2 ** attempt) + (attempt * 0.5)
                    time.sleep(wait_time)
                    continue
                else:
                    logger.debug(f"Failed to upload {filename}: {e}")
                    return False
            except (ssl.SSLError, OSError) as e:
                # SSL/connection error - retry with backoff
                if attempt < max_retries - 1:
                    time.sleep(1 * (attempt + 1))
                    # Reset thread service on SSL error
                    if use_thread_service:
                        self._thread_local.service = None
                        service = self._get_thread_service()
                    continue
                else:
                    logger.debug(f"SSL error uploading {filename}: {e}")
                    return False

        return False

    def get_layers(self) -> List[Dict]:
        """Retrieve all layers from Fulcrum"""
        try:
            response = requests.get(
                f"{self.fulcrum_base_url}/layers.json",
                headers=self.fulcrum_headers,
                timeout=30
            )
            response.raise_for_status()
            layers = response.json().get('layers', [])
            return layers
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch layers: {e}")
            return []

    def export_layers(self) -> int:
        """Export all layers to Google Drive"""
        layers = self.get_layers()

        if not layers:
            return 0

        # Get or create layers folder
        layers_folder_id = self._get_or_create_folder("layers", self.drive_folder_id)

        # List existing layer files
        existing_files = self._list_drive_folder_contents(layers_folder_id)

        uploaded = 0
        for layer in tqdm(layers, desc="Exporting layers", unit="layer"):
            layer_id = layer.get('id')
            layer_name = layer.get('name', 'Unnamed Layer')

            # Sanitize layer name for filename
            safe_name = "".join(c if c.isalnum() or c in (' ', '-', '_') else '_' for c in layer_name)
            filename = f"{safe_name}_{layer_id}.json"

            # Skip if already exists
            if filename in existing_files:
                continue

            # Upload layer as JSON
            layer_json = json.dumps(layer, indent=2, ensure_ascii=False).encode('utf-8')
            if self._upload_to_drive(layer_json, filename, layers_folder_id, "application/json"):
                uploaded += 1

        # Create layers manifest
        manifest = {
            "export_date": datetime.now(UTAH_TZ).isoformat(),
            "total_layers": len(layers),
            "layers": [{"id": l.get('id'), "name": l.get('name')} for l in layers]
        }
        manifest_json = json.dumps(manifest, indent=2, ensure_ascii=False).encode('utf-8')
        self._delete_file_if_exists("LAYERS_MANIFEST.json", layers_folder_id)
        self._upload_to_drive(manifest_json, "LAYERS_MANIFEST.json", layers_folder_id, "application/json")

        self.stats["layers_uploaded"] = uploaded
        return uploaded

    def update_failed_downloads_summary(self) -> None:
        """Update the master failed downloads summary in Google Drive.
        If no forms have failures, deletes the summary file."""
        if not self.failed_forms:
            self._delete_file_if_exists("FAILED_DOWNLOADS_SUMMARY.txt", self.drive_folder_id)
            return

        # Sort by failed count (most failures first)
        sorted_forms = sorted(self.failed_forms, key=lambda x: x['photos_failed'], reverse=True)
        total_failed = sum(f['photos_failed'] for f in sorted_forms)

        summary_lines = [
            "=" * 70,
            "FAILED PHOTO DOWNLOADS SUMMARY",
            "=" * 70,
            "",
            f"Generated: {datetime.now(UTAH_TZ).isoformat()}",
            f"Total Forms with Failed Photos: {len(sorted_forms)}",
            f"Total Photos Failed: {total_failed}",
            "",
            "-" * 70,
            "FORMS WITH FAILED PHOTO DOWNLOADS",
            "-" * 70,
            ""
        ]

        for idx, form_info in enumerate(sorted_forms, 1):
            summary_lines.extend([
                f"{idx}. {form_info['name']}",
                f"   Status: {form_info['status']}",
                f"   Total Photos in Fulcrum: {form_info['photos_in_fulcrum']}",
                f"   Total Photos in Drive: {form_info['photos_in_drive']}",
                f"   Photos Failed: {form_info['photos_failed']}",
                f"   Location: {form_info['path']}/FORM_SUMMARY.txt",
                ""
            ])

        summary_lines.extend([
            "-" * 70,
            "NOTE",
            "-" * 70,
            "",
            "Failed photos are typically due to:",
            "- Photos not yet synced to Fulcrum servers",
            "- Photos that were deleted",
            "- Network timeouts during download",
            "",
            "This summary updates after each form is processed.",
            "=" * 70
        ])

        summary_content = "\n".join(summary_lines)
        self._delete_file_if_exists("FAILED_DOWNLOADS_SUMMARY.txt", self.drive_folder_id)
        self._upload_to_drive(
            summary_content.encode(),
            "FAILED_DOWNLOADS_SUMMARY.txt",
            self.drive_folder_id,
            "text/plain"
        )

    def get_forms(self, since_date: str = None) -> List[Dict]:
        """Get forms from Fulcrum API"""
        logger.debug("Fetching forms from Fulcrum...")

        if since_date:
            query = f"""
                SELECT * FROM forms
                WHERE form_id IN (
                    SELECT DISTINCT form_id FROM changesets WHERE created_at >= '{since_date}'
                )
                OR updated_at >= '{since_date}'
                ORDER BY name
            """
        else:
            query = "SELECT * FROM forms ORDER BY name"

        response = requests.get(
            f"{self.fulcrum_base_url}/query",
            headers=self.fulcrum_headers,
            params={"q": query, "format": "json"},
            timeout=60
        )
        response.raise_for_status()
        data = response.json()

        forms = []
        if 'rows' in data:
            for row in data['rows']:
                if 'form_id' in row and 'id' not in row:
                    row['id'] = row['form_id']
                row['_is_active'] = (row.get('status') == 'active')
                forms.append(row)

        return forms

    def get_form_schema(self, form_id: str) -> Dict:
        """Fetch the full form schema including field definitions"""
        try:
            response = requests.get(
                f"{self.fulcrum_base_url}/forms/{form_id}.json",
                headers=self.fulcrum_headers,
                timeout=30
            )
            response.raise_for_status()
            return response.json().get('form', {})
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch schema for form {form_id}: {e}")
            return {}

    def build_field_mapping(self, form_schema: Dict) -> Dict[str, str]:
        """Build mapping from field keys to human-readable labels"""
        field_map = {}

        def process_elements(elements, parent_key='', parent_label=''):
            for element in elements:
                key = element.get('key') or element.get('data_name')
                label = element.get('label', '')
                elem_type = element.get('type', '')

                # Skip Section and Label elements - they're not data fields
                if elem_type in ['Section', 'Label']:
                    # Still process nested elements within sections
                    if element.get('elements'):
                        process_elements(element.get('elements', []), parent_key, parent_label)
                    continue

                if key:
                    # Build full label with parent context if nested
                    if parent_label and label:
                        full_label = f"{parent_label} - {label}"
                    elif label:
                        full_label = label
                    else:
                        # Fallback: make field key more readable
                        full_label = key.replace('_', ' ').title()

                    # Handle address field sub-components
                    if elem_type == 'AddressField':
                        # Map address sub-fields only, skip the parent
                        for sub in ['sub_thoroughfare', 'thoroughfare', 'suite',
                                   'locality', 'admin_area', 'sub_admin_area', 'postal_code', 'country']:
                            field_map[f"{key}_{sub}"] = f"{full_label} - {sub.replace('_', ' ').title()}"

                    # Handle video fields (caption and video_id sub-fields)
                    elif elem_type == 'VideoField':
                        field_map[key] = full_label
                        field_map[f"{key}_caption"] = f"{full_label} - Caption"
                        field_map[f"{key}_video_id"] = f"{full_label} - Video ID"

                    # Handle photo fields (caption sub-field)
                    elif elem_type == 'PhotoField':
                        field_map[key] = full_label
                        field_map[f"{key}_caption"] = f"{full_label} - Caption"

                    else:
                        # Use label as column name for all other fields
                        full_key = f"{parent_key}_{key}" if parent_key else key
                        field_map[key] = full_label
                        field_map[full_key] = full_label

                # Recursively process nested elements (Repeatables, etc.)
                if element.get('elements') and elem_type not in ['Section', 'Label']:
                    nested_key = key if key and elem_type in ['Repeatable'] else parent_key
                    nested_label = full_label if elem_type in ['Repeatable'] else parent_label
                    process_elements(element.get('elements', []), nested_key, nested_label)

        elements = form_schema.get('elements', [])
        process_elements(elements)

        return field_map

    def flatten_record_for_csv(self, record: Dict, field_mapping: Dict[str, str]) -> Dict:
        """Flatten nested record structure for CSV export using schema-based labels"""
        flat_record = {
            'fulcrum_id': record.get('id'),
            'status': record.get('status'),
            'created_at': record.get('created_at'),
            'updated_at': record.get('updated_at'),
            'created_by': record.get('created_by'),
            'updated_by': record.get('updated_by'),
            'latitude': record.get('latitude'),
            'longitude': record.get('longitude'),
            'altitude': record.get('altitude'),
            'horizontal_accuracy': record.get('horizontal_accuracy'),
            'vertical_accuracy': record.get('vertical_accuracy'),
        }

        # Add form values with human-readable column names
        form_values = record.get('form_values', {})

        def flatten_dict(d, parent_key=''):
            items = []
            for k, v in d.items():
                new_key = f"{parent_key}_{k}" if parent_key else k

                # Get human-readable label from field mapping with better fallbacks
                column_name = field_mapping.get(new_key)
                if not column_name:
                    column_name = field_mapping.get(k)
                if not column_name:
                    # Final fallback: make the key readable
                    column_name = new_key.replace('_', ' ').title()

                if isinstance(v, dict):
                    # Check if it's a photo field
                    if 'photo_id' in v:
                        # Store just the photo_id
                        items.append((column_name, v.get('photo_id', '')))
                        # Handle photo caption if present
                        if 'caption' in v:
                            caption_column = field_mapping.get(f"{k}_caption", f"{column_name} - Caption")
                            items.append((caption_column, v.get('caption', '')))
                    # Check if it's a video field
                    elif 'video_id' in v:
                        # Store video_id
                        video_column = field_mapping.get(f"{k}_video_id", f"{column_name} - Video ID")
                        items.append((video_column, v.get('video_id', '')))
                        # Handle video caption if present
                        if 'caption' in v:
                            caption_column = field_mapping.get(f"{k}_caption", f"{column_name} - Caption")
                            items.append((caption_column, v.get('caption', '')))
                    # Check if it's a choice field with choice_values
                    elif 'choice_values' in v:
                        # Extract just the choice values, not the full dict
                        choice_vals = v.get('choice_values', [])
                        if isinstance(choice_vals, list):
                            items.append((column_name, ', '.join(choice_vals)))
                        else:
                            items.append((column_name, choice_vals))
                    # Check if it's an address field
                    elif any(key in v for key in ['sub_thoroughfare', 'thoroughfare', 'locality']):
                        # Flatten address sub-fields with better fallback
                        for addr_key, addr_val in v.items():
                            addr_column = field_mapping.get(f"{k}_{addr_key}")
                            if not addr_column:
                                addr_column = f"{column_name} - {addr_key.replace('_', ' ').title()}"
                            items.append((addr_column, addr_val))
                    else:
                        # Regular nested dict - recurse
                        items.extend(flatten_dict(v, new_key).items())
                elif isinstance(v, list):
                    # Check if it's a list of photo objects
                    if v and isinstance(v[0], dict) and 'photo_id' in v[0]:
                        # Comma-separated photo IDs
                        photo_ids = [p.get('photo_id', '') for p in v if p.get('photo_id')]
                        items.append((column_name, ', '.join(photo_ids)))
                    # Check if it's a repeatable section
                    elif v and isinstance(v[0], dict):
                        # For repeatable sections, create indexed columns
                        for idx, item in enumerate(v):
                            if isinstance(item, dict):
                                for sub_k, sub_v in flatten_dict(item, new_key).items():
                                    items.append((f"{sub_k}_{idx+1}", sub_v))
                    else:
                        # Simple list of values
                        items.append((column_name, ', '.join(map(str, v))))
                else:
                    items.append((column_name, v))
            return dict(items)

        flat_values = flatten_dict(form_values)
        flat_record.update(flat_values)

        return flat_record

    def get_records(self, form_id: str) -> List[Dict]:
        """Get all records for a form"""
        all_records = []
        page = 1

        while True:
            response = requests.get(
                f"{self.fulcrum_base_url}/records.json",
                headers=self.fulcrum_headers,
                params={"form_id": form_id, "page": page, "per_page": 10000},
                timeout=60
            )
            response.raise_for_status()
            data = response.json()
            records = data.get('records', [])

            if not records:
                break

            all_records.extend(records)
            page += 1

        return all_records

    def extract_photo_ids(self, record: Dict) -> List[Dict]:
        """Extract all photo IDs from a record with field context"""
        photos = []
        form_values = record.get('form_values', {})

        def recurse(values, path=''):
            if isinstance(values, dict):
                if 'photo_id' in values:
                    photos.append({
                        'photo_id': values['photo_id'],
                        'record_id': record['id'],
                        'field_path': path,
                        'caption': values.get('caption', '')
                    })
                else:
                    for key, value in values.items():
                        recurse(value, f"{path}.{key}" if path else key)
            elif isinstance(values, list):
                for idx, item in enumerate(values):
                    recurse(item, f"{path}[{idx}]")

        recurse(form_values)
        return photos

    def get_photos_metadata_batch(self, form_id: str) -> Dict[str, Dict]:
        """Batch fetch all photo metadata for a form - much faster than individual calls"""
        all_photos = {}
        page = 1

        while True:
            try:
                response = requests.get(
                    f"{self.fulcrum_base_url}/photos.json",
                    headers=self.fulcrum_headers,
                    params={"form_id": form_id, "page": page, "per_page": 20000},
                    timeout=60
                )
                response.raise_for_status()
                data = response.json()
                photos = data.get('photos', [])

                if not photos:
                    break

                for photo in photos:
                    photo_id = photo.get('access_key')
                    if photo_id:
                        all_photos[photo_id] = photo

                page += 1

                # Check if we got all pages
                if len(photos) < 20000:
                    break

            except Exception as e:
                logger.warning(f"Failed to batch fetch photos page {page}: {e}")
                break

        return all_photos

    def download_photo_with_metadata(self, photo_id: str, photo_data: Dict, max_retries: int = 3) -> tuple:
        """Download photo using direct /photos/{id}.jpg endpoint (3x faster than CDN URLs)
        Uses batch metadata for validation only, downloads via direct API endpoint"""
        last_error = None

        # Validate using batch metadata
        if photo_data.get('deleted_at'):
            return None, None, "Photo deleted", None
        if not photo_data.get('stored'):
            return None, None, "Photo not stored", None
        if not photo_data.get('processed'):
            return None, None, "Photo not processed", None

        # Determine extension from content_type
        content_type = photo_data.get('content_type', '')
        ext = 'png' if 'png' in content_type else 'jpg'

        # Use direct endpoint - much faster than CDN URLs (tested: 3x speedup)
        direct_url = f"{self.fulcrum_base_url}/photos/{photo_id}.jpg"

        for attempt in range(max_retries):
            try:
                photo_response = requests.get(direct_url, headers=self.fulcrum_headers, timeout=60)
                photo_response.raise_for_status()
                return photo_response.content, ext, None, photo_data

            except (ssl.SSLError, requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
                last_error = str(e)
                if attempt < max_retries - 1:
                    time.sleep(1 * (attempt + 1))
                    continue
            except Exception as e:
                return None, None, str(e), None

        return None, None, f"SSL/Connection error after {max_retries} retries: {last_error}", None

    def download_photo_to_memory(self, photo_id: str, max_retries: int = 3) -> tuple:
        """Download a photo from Fulcrum to memory (fallback when no batch metadata)
        Uses direct /photos/{id}.jpg endpoint for speed"""
        last_error = None

        for attempt in range(max_retries):
            try:
                # Get photo metadata first (needed for validation and CSV)
                response = requests.get(
                    f"{self.fulcrum_base_url}/photos/{photo_id}.json",
                    headers=self.fulcrum_headers,
                    timeout=30
                )
                response.raise_for_status()
                photo_data = response.json().get('photo', {})

                if photo_data.get('deleted_at'):
                    return None, None, "Photo deleted", None
                if not photo_data.get('stored'):
                    return None, None, "Photo not stored", None
                if not photo_data.get('processed'):
                    return None, None, "Photo not processed", None

                # Determine extension
                content_type = photo_data.get('content_type', '')
                ext = 'png' if 'png' in content_type else 'jpg'

                # Download via direct endpoint (faster than CDN URL)
                direct_url = f"{self.fulcrum_base_url}/photos/{photo_id}.jpg"
                photo_response = requests.get(direct_url, headers=self.fulcrum_headers, timeout=60)
                photo_response.raise_for_status()

                return photo_response.content, ext, None, photo_data

            except (ssl.SSLError, requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
                last_error = str(e)
                if attempt < max_retries - 1:
                    time.sleep(1 * (attempt + 1))
                    continue
            except Exception as e:
                return None, None, str(e), None

        return None, None, f"SSL/Connection error after {max_retries} retries: {last_error}", None

    def _upload_single_geojson(self, record: Dict, folder_id: str) -> bool:
        """Upload a single record as GeoJSON to Drive (thread-safe)"""
        record_id = record.get('id')
        filename = f"{record_id}.json"
        record_json = json.dumps(record, indent=2, ensure_ascii=False).encode('utf-8')
        return self._upload_to_drive(record_json, filename, folder_id, "application/json", use_thread_service=True)

    def _upload_geojson_concurrent(self, records: List[Dict], folder_id: str, existing_files: Set[str], max_workers: int = 5) -> int:
        """Upload GeoJSON files concurrently, returns count of uploaded files"""
        # Filter to only records that need uploading
        to_upload = [r for r in records if f"{r.get('id')}.json" not in existing_files]

        if not to_upload:
            return 0

        uploaded = 0
        completed_count = 0
        timeout_seconds = 60  # 1 minute timeout per record
        token_refresh_interval = 100

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_record = {
                executor.submit(self._upload_single_geojson, record, folder_id): record
                for record in to_upload
            }

            with tqdm(total=len(to_upload), desc="    GeoJSON", unit="record", leave=False) as pbar:
                try:
                    # Add timeout to as_completed to detect stalls (5 min max wait for any completion)
                    for future in as_completed(future_to_record, timeout=300):
                        try:
                            if future.result(timeout=timeout_seconds):
                                uploaded += 1
                        except TimeoutError:
                            logger.debug(f"GeoJSON upload timed out")
                        except Exception as e:
                            logger.debug(f"GeoJSON upload failed: {e}")

                        completed_count += 1
                        pbar.update(1)

                        # Periodic token refresh
                        if completed_count % token_refresh_interval == 0:
                            elapsed = time.time() - self._last_token_refresh
                            if elapsed > 30 * 60:
                                self._refresh_drive_token_if_needed(force=True)
                except TimeoutError:
                    # as_completed timed out - some futures are stuck
                    pending = len(to_upload) - completed_count
                    logger.warning(f"GeoJSON upload stalled - {pending} uploads stuck, cancelling")
                    for future in future_to_record:
                        future.cancel()

        return uploaded

    def export_form(self, form: Dict) -> Dict:
        """Export a single form to Google Drive"""
        form_id = form['id']
        form_name = form['name']
        is_active = form.get('_is_active', True)

        # Sanitize form name and append form ID for uniqueness
        safe_name = "".join(c if c.isalnum() or c in (' ', '-', '_') else '_' for c in form_name)
        safe_name = f"{safe_name}_{form_id}"

        logger.debug(f"Processing form: {form_name}")

        # Get or create form folder in Drive
        parent_id = self.active_forms_id if is_active else self.inactive_forms_id
        form_folder_id = self._get_or_create_folder(safe_name, parent_id)

        # Get records
        records = self.get_records(form_id)
        logger.debug(f"  Found {len(records)} records")

        if not records:
            # Clear all old data from the folder since this form has no records
            cleared = self._clear_folder_contents(form_folder_id)
            if cleared > 0:
                logger.info(f"  Cleared {cleared} old files from empty form folder")
            self._upload_to_drive(b"This form has no records in Fulcrum.", "NO_RECORDS.txt", form_folder_id, "text/plain")
            self.stats["forms_processed"] += 1
            return {"form": form_name, "records": 0, "photos_uploaded": 0, "photos_skipped": 0, "photos_failed": 0, "geojson_uploaded": 0}

        # Get form schema for proper field labels
        form_schema = self.get_form_schema(form_id)
        field_mapping = self.build_field_mapping(form_schema)

        # Build CSV in memory with proper flattening
        all_flat_records = []

        for record in records:
            flat_record = self.flatten_record_for_csv(record, field_mapping)
            all_flat_records.append(flat_record)

        # Collect all keys from records
        all_keys = set()
        for record in all_flat_records:
            all_keys.update(record.keys())

        # Add all field labels from schema to ensure empty fields are included
        for label in field_mapping.values():
            all_keys.add(label)

        # Define column order: system fields first, then form fields alphabetically, then photo fields last
        system_fields = [
            'fulcrum_id',
            'status',
            'latitude',
            'longitude',
            'altitude',
            'horizontal_accuracy',
            'vertical_accuracy',
            'created_at',
            'updated_at',
            'created_by',
            'updated_by'
        ]

        # Separate form fields into photo fields and non-photo fields
        non_system_fields = [k for k in all_keys if k not in system_fields]
        photo_fields = sorted([k for k in non_system_fields if 'photo' in k.lower() or 'image' in k.lower()])
        non_photo_fields = sorted([k for k in non_system_fields if k not in photo_fields])

        # Build final column order: system fields, non-photo form fields, then photo fields
        fieldnames = [f for f in system_fields if f in all_keys] + non_photo_fields + photo_fields

        csv_buffer = io.StringIO()
        writer = csv.DictWriter(csv_buffer, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(all_flat_records)

        # Upload CSV (delete existing first to replace)
        csv_filename = f"{safe_name}_data.csv"
        self._delete_file_if_exists(csv_filename, form_folder_id)
        csv_content = csv_buffer.getvalue().encode('utf-8')
        self._upload_to_drive(csv_content, csv_filename, form_folder_id, "text/csv")

        # Get or create geojson folder and upload each record as JSON
        geojson_folder_id = self._get_or_create_folder("geojson", form_folder_id)
        existing_geojson = self._list_drive_folder_contents(geojson_folder_id)

        # Refresh token before concurrent uploads if needed
        self._refresh_drive_token_if_needed()
        geojson_uploaded = self._upload_geojson_concurrent(records, geojson_folder_id, existing_geojson)

        # Get or create photos folder
        photos_folder_id = self._get_or_create_folder("photos", form_folder_id)

        # Collect all photos from records
        all_photos = []
        for record in records:
            all_photos.extend(self.extract_photo_ids(record))

        # Check existing photos
        existing_photos = self._list_drive_folder_contents(photos_folder_id)

        # Filter to only missing photos (check Drive AND progress file)
        photos_to_download = []
        for photo in all_photos:
            photo_id = photo['photo_id']
            # Skip if already in Drive
            if f"{photo_id}.jpg" in existing_photos or f"{photo_id}.png" in existing_photos:
                continue
            # Skip if uploaded in a previous run (from progress file)
            if self._is_photo_uploaded(photo_id):
                continue
            photos_to_download.append(photo)

        skipped = len(all_photos) - len(photos_to_download)

        # Batch-fetch all photo metadata for this form (needed for downloads AND cleanup)
        photo_metadata_cache = self.get_photos_metadata_batch(form_id)

        # Clean up photos that were deleted in Fulcrum (with Slack approval if enabled)
        deleted_count, cleanup_action = self.cleanup_deleted_photos(photos_folder_id, photo_metadata_cache, form_name)
        if cleanup_action == 'ended':
            # User requested to end export via Slack
            return {"form": form_name, "records": len(records), "photos_uploaded": 0, "photos_skipped": skipped, "photos_failed": 0, "photos_deleted": 0, "geojson_uploaded": geojson_uploaded, "export_ended": True}

        # Download and upload missing photos with retry logic
        failed_photos = []
        uploaded = 0
        all_photo_results = []

        if photos_to_download:
            # Refresh token before concurrent uploads if needed
            self._refresh_drive_token_if_needed()

            # First attempt
            all_photo_results, failed_photos = self._download_and_upload_photos(photos_to_download, photos_folder_id, photo_metadata_cache)
            uploaded = len(photos_to_download) - len(failed_photos)

            # Retry logic - up to 3 attempts for same failures
            retry_count = 0
            previous_failed_count = len(failed_photos)

            while failed_photos and retry_count < 3:
                retry_count += 1
                logger.debug(f"  Retry attempt {retry_count} for {len(failed_photos)} failed photos...")

                retry_results, still_failed = self._download_and_upload_photos(failed_photos, photos_folder_id, photo_metadata_cache)
                newly_uploaded = len(failed_photos) - len(still_failed)
                uploaded += newly_uploaded

                # Add successful retries to all_photo_results
                for result in retry_results:
                    if result.get('success'):
                        all_photo_results.append(result)

                if len(still_failed) >= previous_failed_count:
                    # No progress made
                    if retry_count >= 2:
                        logger.debug(f"  No progress after {retry_count} retries, giving up on {len(still_failed)} photos")
                        break
                else:
                    previous_failed_count = len(still_failed)

                failed_photos = still_failed

        # Build and upload photos CSVs (delete existing first to replace)
        if all_photo_results:
            all_csv, before_csv, completed_csv = self.build_photos_csv(all_photo_results)

            if all_csv:
                photos_csv_name = f"{safe_name}_photos.csv"
                self._delete_file_if_exists(photos_csv_name, form_folder_id)
                self._upload_to_drive(all_csv, photos_csv_name, form_folder_id, "text/csv")

            if before_csv:
                before_csv_name = f"{safe_name}_before_photos.csv"
                self._delete_file_if_exists(before_csv_name, form_folder_id)
                self._upload_to_drive(before_csv, before_csv_name, form_folder_id, "text/csv")

            if completed_csv:
                completed_csv_name = f"{safe_name}_completed_photos.csv"
                self._delete_file_if_exists(completed_csv_name, form_folder_id)
                self._upload_to_drive(completed_csv, completed_csv_name, form_folder_id, "text/csv")

        # Count actual photos in Drive after cleanup and uploads (bypass cache - contents changed)
        photos_in_drive = len(self._list_drive_folder_contents(photos_folder_id, use_cache=False))

        # Create summary
        summary_lines = [
            "=" * 60,
            "FORM EXPORT SUMMARY",
            "=" * 60,
            "",
            f"Form Name: {form_name}",
            f"Status: {'ACTIVE' if is_active else 'INACTIVE'}",
            f"Form ID: {form_id}",
            f"Export Date: {datetime.now(UTAH_TZ).isoformat()}",
            "",
            "-" * 60,
            "STATISTICS",
            "-" * 60,
            "",
            f"Total Records: {len(records)}",
            f"GeoJSON Files: {len(records)}",
            f"Total Photos in Fulcrum: {len(all_photos)}",
            f"Total Photos in Drive: {photos_in_drive}",
            f"Photos Failed: {len(failed_photos)}",
            "",
            "-" * 60,
            "EXPORTED FILES",
            "-" * 60,
            "",
            f"- {safe_name}_data.csv",
            f"- geojson/ ({len(records)} JSON files)",
            f"- photos/ ({photos_in_drive} image files)",
        ]

        if all_photo_results:
            summary_lines.append(f"- {safe_name}_photos.csv")
            # Check if before/completed CSVs were created
            before_count = sum(1 for r in all_photo_results if r.get('success') and 'before' in r.get('field_path', '').lower())
            completed_count = sum(1 for r in all_photo_results if r.get('success') and any(x in r.get('field_path', '').lower() for x in ['completed', 'after', 'complete']))
            if before_count > 0:
                summary_lines.append(f"- {safe_name}_before_photos.csv ({before_count} photos)")
            if completed_count > 0:
                summary_lines.append(f"- {safe_name}_completed_photos.csv ({completed_count} photos)")

        summary_lines.append("")

        if failed_photos:
            summary_lines.extend([
                "-" * 60,
                "FAILED PHOTOS",
                "-" * 60,
                ""
            ])
            for photo in failed_photos:
                summary_lines.append(f"Photo ID: {photo['photo_id']}")
                summary_lines.append(f"Record ID: {photo['record_id']}")
                summary_lines.append(f"Error: {photo.get('error', 'Unknown')}")
                summary_lines.append("")

        summary_content = "\n".join(summary_lines)
        self._delete_file_if_exists("FORM_SUMMARY.txt", form_folder_id)
        self._upload_to_drive(summary_content.encode(), "FORM_SUMMARY.txt", form_folder_id, "text/plain")

        # Update stats
        self.stats["forms_processed"] += 1
        self.stats["photos_uploaded"] += uploaded
        self.stats["photos_skipped"] += skipped
        self.stats["photos_failed"] += len(failed_photos)
        self.stats["photos_deleted"] += deleted_count
        self.stats["geojson_uploaded"] += geojson_uploaded
        self.stats["total_records"] += len(records)

        # Update failed forms tracking (remove old entry for this form first)
        self.failed_forms = [f for f in self.failed_forms if f['name'] != form_name]
        if failed_photos:
            folder_type = "active_forms" if is_active else "inactive_forms"
            self.failed_forms.append({
                "name": form_name,
                "status": "ACTIVE" if is_active else "INACTIVE",
                "photos_in_fulcrum": len(all_photos),
                "photos_in_drive": photos_in_drive,
                "photos_failed": len(failed_photos),
                "path": f"{folder_type}/{safe_name}"
            })
        # Always update the summary (removes file if no failures remain)
        self.update_failed_downloads_summary()

        return {
            "form": form_name,
            "records": len(records),
            "photos_uploaded": uploaded,
            "photos_skipped": skipped,
            "photos_failed": len(failed_photos),
            "photos_deleted": deleted_count,
            "geojson_uploaded": geojson_uploaded
        }

    def _process_single_photo(self, photo: Dict, photos_folder_id: str, photo_metadata_cache: Dict = None) -> Dict:
        """Download and upload a single photo, returns photo dict with error if failed, or success dict with metadata"""
        photo_id = photo['photo_id']

        # Use cached metadata if available (much faster), otherwise fetch individually
        if photo_metadata_cache and photo_id in photo_metadata_cache:
            content, ext, error, photo_data = self.download_photo_with_metadata(photo_id, photo_metadata_cache[photo_id])
        else:
            content, ext, error, photo_data = self.download_photo_to_memory(photo_id)

        if error:
            photo['error'] = error
            photo['success'] = False
            return photo

        filename = f"{photo_id}.{ext}"
        mime_type = "image/png" if ext == "png" else "image/jpeg"

        if not self._upload_to_drive(content, filename, photos_folder_id, mime_type, use_thread_service=True):
            photo['error'] = "Upload failed"
            photo['success'] = False
            return photo

        # Success - record upload and return photo info with metadata for CSV
        self._record_photo_uploaded(photo_id)
        photo['success'] = True
        photo['photo_data'] = photo_data
        photo['filename'] = filename
        return photo

    def _download_and_upload_photos(self, photos: List[Dict], photos_folder_id: str, photo_metadata_cache: Dict = None, max_workers: int = 5) -> tuple:
        """Download photos from Fulcrum and upload to Drive concurrently
        Returns: (all_results, failed_photos) where all_results includes metadata for CSV"""
        all_results = []
        failed = []
        completed_count = 0
        timeout_seconds = 120  # 2 minute timeout per photo
        token_refresh_interval = 100  # Refresh token every 100 photos
        progress_log_interval = 100  # Log progress every N photos
        last_progress_log = time.time()
        progress_time_interval = 60  # Also log at least every 60 seconds

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_photo = {
                executor.submit(self._process_single_photo, photo, photos_folder_id, photo_metadata_cache): photo
                for photo in photos
            }

            with tqdm(total=len(photos), desc="    Photos", unit="photo", leave=False) as pbar:
                try:
                    # Add timeout to as_completed to detect stalls (5 min max wait for any completion)
                    for future in as_completed(future_to_photo, timeout=300):
                        photo = future_to_photo[future]
                        try:
                            result = future.result(timeout=timeout_seconds)
                            all_results.append(result)
                            if not result.get('success', False):
                                failed.append(result)
                        except TimeoutError:
                            # Photo upload timed out - mark as failed
                            logger.warning(f"Photo {photo.get('photo_id')} timed out after {timeout_seconds}s")
                            photo['error'] = f"Timeout after {timeout_seconds}s"
                            photo['success'] = False
                            all_results.append(photo)
                            failed.append(photo)
                        except Exception as e:
                            logger.warning(f"Photo {photo.get('photo_id')} failed: {e}")
                            photo['error'] = str(e)
                            photo['success'] = False
                            all_results.append(photo)
                            failed.append(photo)

                        completed_count += 1
                        pbar.update(1)

                        # Log progress periodically to keep GitHub Actions log active
                        now = time.time()
                        if completed_count % progress_log_interval == 0 or (now - last_progress_log) >= progress_time_interval:
                            pct = (completed_count / len(photos)) * 100 if photos else 0
                            logger.info(f"    Photo progress: {completed_count}/{len(photos)} ({pct:.0f}%)")
                            last_progress_log = now

                        # Periodic token refresh during long upload batches
                        if completed_count % token_refresh_interval == 0:
                            elapsed = time.time() - self._last_token_refresh
                            if elapsed > 30 * 60:  # Refresh if > 30 min since last refresh
                                self._refresh_drive_token_if_needed(force=True)
                except TimeoutError:
                    # as_completed timed out - all workers are stuck
                    pending_futures = [f for f in future_to_photo if not f.done()]
                    logger.warning(f"Photo uploads stalled - {len(pending_futures)} photos stuck, marking as failed")
                    for future in pending_futures:
                        photo = future_to_photo[future]
                        photo['error'] = "Stalled (worker hung)"
                        photo['success'] = False
                        all_results.append(photo)
                        failed.append(photo)
                        future.cancel()

        return all_results, failed

    def build_photos_csv(self, photo_results: List[Dict]) -> tuple:
        """Build photos CSV content from photo results
        Returns: (all_photos_csv, before_photos_csv, completed_photos_csv) as bytes"""

        # Define CSV fieldnames matching original export
        fieldnames = [
            'fulcrum_id', 'fulcrum_parent_id', 'fulcrum_record_id', 'version', 'caption',
            'latitude', 'longitude', 'geometry', 'file_size', 'uploaded_at', 'created_at', 'updated_at',
            'content_type', 'stored_size',
            'exif_date_time', 'exif_gps_altitude', 'exif_gps_date_stamp', 'exif_gps_time_stamp',
            'exif_gps_dop', 'exif_gps_img_direction', 'exif_gps_img_direction_ref',
            'exif_gps_latitude', 'exif_gps_latitude_ref', 'exif_gps_longitude', 'exif_gps_longitude_ref',
            'exif_make', 'exif_model', 'exif_orientation', 'exif_pixel_x_dimension', 'exif_pixel_y_dimension',
            'exif_software', 'exif_x_resolution', 'exif_y_resolution'
        ]

        all_rows = []
        before_rows = []
        completed_rows = []

        for result in photo_results:
            if not result.get('success'):
                continue

            photo_data = result.get('photo_data', {})
            if not photo_data:
                continue

            # Extract EXIF data
            exif = photo_data.get('exif', {}) or {}

            row = {
                'fulcrum_id': photo_data.get('access_key'),
                'fulcrum_parent_id': photo_data.get('record_id'),
                'fulcrum_record_id': result.get('record_id'),
                'version': photo_data.get('updated_at'),
                'caption': result.get('caption', ''),
                'latitude': photo_data.get('latitude'),
                'longitude': photo_data.get('longitude'),
                'geometry': f"POINT ({photo_data.get('longitude')} {photo_data.get('latitude')})" if photo_data.get('latitude') and photo_data.get('longitude') else '',
                'file_size': photo_data.get('file_size'),
                'uploaded_at': photo_data.get('uploaded_at'),
                'created_at': photo_data.get('created_at'),
                'updated_at': photo_data.get('updated_at'),
                'content_type': photo_data.get('content_type'),
                'stored_size': photo_data.get('stored_size'),
                # EXIF fields
                'exif_date_time': exif.get('date_time'),
                'exif_gps_altitude': exif.get('gps_altitude'),
                'exif_gps_date_stamp': exif.get('gps_date_stamp'),
                'exif_gps_time_stamp': exif.get('gps_time_stamp'),
                'exif_gps_dop': exif.get('gps_dop'),
                'exif_gps_img_direction': exif.get('gps_img_direction'),
                'exif_gps_img_direction_ref': exif.get('gps_img_direction_ref'),
                'exif_gps_latitude': exif.get('gps_latitude'),
                'exif_gps_latitude_ref': exif.get('gps_latitude_ref'),
                'exif_gps_longitude': exif.get('gps_longitude'),
                'exif_gps_longitude_ref': exif.get('gps_longitude_ref'),
                'exif_make': exif.get('make'),
                'exif_model': exif.get('model'),
                'exif_orientation': exif.get('orientation'),
                'exif_pixel_x_dimension': exif.get('pixel_x_dimension'),
                'exif_pixel_y_dimension': exif.get('pixel_y_dimension'),
                'exif_software': exif.get('software'),
                'exif_x_resolution': exif.get('x_resolution'),
                'exif_y_resolution': exif.get('y_resolution'),
            }

            all_rows.append(row)

            # Categorize by field path
            field_path = result.get('field_path', '').lower()
            if 'before' in field_path:
                before_rows.append(row)
            elif 'completed' in field_path or 'after' in field_path or 'complete' in field_path:
                completed_rows.append(row)

        # Build CSV content
        def rows_to_csv(rows):
            if not rows:
                return None
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
            return buffer.getvalue().encode('utf-8')

        return rows_to_csv(all_rows), rows_to_csv(before_rows), rows_to_csv(completed_rows)

    def export_all(self, since_date: str = None, test_mode: bool = False, max_forms: int = 3):
        """Export all forms to Google Drive"""
        # Set export start time for timeout tracking
        self._export_start_time = time.time()
        self._forms_list = None  # Will be set later for signal handler

        # Set up signal handler for graceful shutdown (e.g., GitHub Actions timeout)
        def handle_shutdown(signum, frame):
            logger.warning(f"Received shutdown signal ({signum}), saving state...")
            if self._forms_list:
                self._save_export_state(self._current_form_index, self._forms_list)
            self._save_photo_progress()
            logger.info("State saved. Re-run workflow to continue.")
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, handle_shutdown)
        signal.signal(signal.SIGINT, handle_shutdown)

        # Also register atexit handler as backup
        def save_on_exit():
            if self._forms_list and self._current_form_index < len(self._forms_list):
                self._save_photo_progress()

        atexit.register(save_on_exit)

        # Check for continuation from previous run
        previous_state = self._load_export_state()
        start_form_index = 0
        if previous_state:
            start_form_index = previous_state.get('form_index', 0)
            # Restore stats from previous run
            prev_stats = previous_state.get('stats', {})
            for key in prev_stats:
                if key in self.stats:
                    self.stats[key] = prev_stats[key]
            logger.info(f"Resuming export from form {start_form_index} (previous stats restored)")
            if self.slack_enabled:
                self.send_slack_message(f"🔄 *Fulcrum Export Resuming* from form {start_form_index}")
        else:
            logger.info("Starting Fulcrum to Google Drive export...")
            if self.slack_enabled:
                self.send_slack_message("🚀 *Fulcrum Export Started*")

        # Load photo progress for resume functionality
        self._load_photo_progress()

        # Initialize Google Drive
        if not self.init_google_drive():
            logger.error("Failed to initialize Google Drive")
            return

        # Pre-load existing folder structure for faster lookups
        self._preload_existing_folders()

        # Export layers (only on fresh start)
        if start_form_index == 0:
            self.export_layers()

        # Get forms
        forms = self.get_forms(since_date)

        if test_mode:
            forms = forms[:max_forms]

        # Store forms list for signal handler
        self._forms_list = forms

        # Flag to track if we hit timeout
        timeout_triggered = False

        # Process each form (starting from continuation point if applicable)
        results = []
        for idx, form in enumerate(forms, 1):
            # Skip forms already processed in previous run
            if idx - 1 < start_form_index:
                continue

            self._current_form_index = idx - 1  # Track for state saving
            # Check for approaching timeout (workflow chaining)
            if self._check_timeout():
                logger.warning(f"Approaching workflow timeout - saving state for continuation")
                self._save_export_state(idx - 1, forms)  # Save current position (0-indexed)
                timeout_triggered = True
                if self.slack_enabled:
                    remaining = len(forms) - (idx - 1)
                    self.send_slack_message(f"⏰ *Export paused* at form {idx-1}/{len(forms)} due to timeout. {remaining} forms remaining. Re-run workflow to continue.")
                break

            # Refresh Google Drive token if needed (prevents timeout after ~1 hour)
            self._refresh_drive_token_if_needed()

            if self._export_cancelled:
                break

            form_name = form['name']
            safe_name = "".join(c if c.isalnum() or c in (' ', '-', '_') else '_' for c in form_name)
            safe_name = f"{safe_name}_{form['id']}"

            # Quick check: skip if form folder already exists
            if self.quick_check:
                parent_id = self.active_forms_id if form.get('_is_active', True) else self.inactive_forms_id
                cache_key = f"{parent_id}/{safe_name}"
                if cache_key in self._folder_cache:
                    self.stats['forms_processed'] += 1
                    continue

            logger.info(f"[{idx}/{len(forms)}] {form_name}")

            # Retry logic for SSL/connection errors
            result = None
            for attempt in range(3):
                try:
                    result = self.export_form(form)
                    results.append(result)
                    break  # Success
                except (ssl.SSLError, OSError) as e:
                    if attempt < 2:
                        logger.warning(f"Connection error, retrying ({attempt + 1}/3)...")
                        time.sleep(5 * (attempt + 1))
                        self._refresh_drive_token_if_needed(force=True)
                    else:
                        logger.error(f"Failed after 3 attempts: {e}")
                        results.append({"form": form['name'], "error": str(e)})
                except Exception as e:
                    logger.error(f"Failed to export form: {e}")
                    results.append({"form": form['name'], "error": str(e)})
                    break

            if result and result.get('export_ended'):
                break

        # Save final photo progress
        self._save_photo_progress()

        # Print summary
        if timeout_triggered:
            status = "PAUSED (timeout)"
        elif self._export_cancelled:
            status = "ENDED EARLY"
        else:
            status = "COMPLETE"
            # Clear state files on successful completion
            self._clear_export_state()

        logger.info(f"\n=== EXPORT {status} ===")
        logger.info(f"Forms: {self.stats['forms_processed']} | Records: {self.stats['total_records']} | Photos: {self.stats['photos_uploaded']} uploaded, {self.stats['photos_skipped']} skipped")
        if self.stats['photos_failed'] > 0:
            logger.warning(f"Failed: {self.stats['photos_failed']} photos in {len(self.failed_forms)} forms")

        # Send completion notification to Slack
        if self.slack_enabled:
            if timeout_triggered:
                status_emoji = "⏰"
                status_text = "Export Paused (timeout - re-run to continue)"
            elif self._export_cancelled:
                status_emoji = "🛑"
                status_text = "Export Ended Early"
            elif self._skipped_forms:
                status_emoji = "⚠️"
                status_text = "Export Complete (with skipped forms)"
            else:
                status_emoji = "✅"
                status_text = "Export Complete"

            message = (
                f"{status_emoji} *{status_text}*\n"
                f"• Forms processed: {self.stats['forms_processed']}\n"
                f"• Records: {self.stats['total_records']}\n"
                f"• Photos uploaded: {self.stats['photos_uploaded']}\n"
                f"• Photos deleted: {self.stats['photos_deleted']}"
            )

            # List skipped forms if any (numbered for easy reference)
            if self._skipped_forms:
                message += "\n\n*Forms with skipped deletions:*"
                for idx, skipped in enumerate(self._skipped_forms, 1):
                    message += f"\n`{idx}` {skipped['form']} ({skipped['photos']} photos)"
                message += "\n\n_Reply `/approve-deletions 1,2,3` to re-run with approvals_"

            self.send_slack_message(message)

        # Save skipped forms to file for re-run with pre-approvals
        if self._skipped_forms:
            try:
                with open(self._skipped_forms_file, 'w') as f:
                    json.dump(self._skipped_forms, f, indent=2)
            except Exception as e:
                logger.debug(f"Failed to save skipped forms: {e}")


def load_pre_approved_forms(pre_approved_arg: str) -> List[str]:
    """Load pre-approved forms from argument or skipped_forms.json

    Accepts:
    - Comma-separated numbers (e.g., "1,2,5") - resolves from skipped_forms.json
    - Comma-separated form names (e.g., "Form A,Form B")
    - Mix of both
    """
    if not pre_approved_arg:
        return []

    skipped_forms_file = SCRIPT_DIR / 'skipped_forms.json'
    skipped_forms = []

    # Try to load previous skipped forms for number resolution
    if skipped_forms_file.exists():
        try:
            with open(skipped_forms_file, 'r') as f:
                skipped_forms = json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load skipped_forms.json: {e}")

    pre_approved = []
    items = [item.strip() for item in pre_approved_arg.split(',')]

    for item in items:
        if item.isdigit():
            # It's a number - resolve from skipped_forms.json
            idx = int(item) - 1  # Convert to 0-based index
            if 0 <= idx < len(skipped_forms):
                form_name = skipped_forms[idx]['form']
                pre_approved.append(form_name)
        else:
            # It's a form name
            pre_approved.append(item)

    return pre_approved


def main():
    fulcrum_token = os.getenv('FULCRUM_API_TOKEN')
    if not fulcrum_token:
        logger.error("FULCRUM_API_TOKEN not found")
        return

    import sys

    # Parse arguments
    since_date = None
    test_mode = '--test' in sys.argv
    skip_deletions = '--skip-deletions' in sys.argv  # Auto-skip all deletion requests
    auto_delete = '--auto-delete' in sys.argv  # Auto-delete all orphaned photos
    quick_check = '--quick-check' in sys.argv  # Skip forms if folder exists
    drive_folder = "Fulcrum-Auto Update/Initial Sync"
    pre_approved_arg = None
    auto_confirm = '--yes' in sys.argv or '-y' in sys.argv

    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == '--since' and i < len(sys.argv) - 1:
            since_date = sys.argv[i + 1]
        elif arg.startswith('--since='):
            since_date = arg.split('=', 1)[1]
        elif arg.startswith('--folder='):
            drive_folder = arg.split('=', 1)[1]
        elif arg.startswith('--pre-approved='):
            pre_approved_arg = arg.split('=', 1)[1]

    # Also check environment variable for pre-approved (useful for GitHub Actions)
    if not pre_approved_arg:
        pre_approved_arg = os.getenv('PRE_APPROVED_FORMS')

    # Resolve pre-approved forms
    pre_approved_forms = load_pre_approved_forms(pre_approved_arg)

    # Log config
    modes = []
    if test_mode:
        modes.append("test")
    if skip_deletions:
        modes.append("skip-deletions")
    if auto_delete:
        modes.append("auto-delete")
    if quick_check:
        modes.append("quick-check")
    mode_str = f" [{', '.join(modes)}]" if modes else ""
    logger.info(f"Folder: {drive_folder}{mode_str}")

    if not auto_confirm:
        response = input("\nProceed with export? (yes/no): ")
        if response.lower() != 'yes':
            return

    exporter = FulcrumToDriveExporter(fulcrum_token, drive_folder, pre_approved_forms=pre_approved_forms, skip_deletions=skip_deletions, auto_delete=auto_delete, quick_check=quick_check)
    exporter.export_all(since_date=since_date, test_mode=test_mode)


if __name__ == "__main__":
    main()
