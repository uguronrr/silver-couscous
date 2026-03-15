import os
import requests
from PIL import Image
from io import BytesIO
import config
import storage
from rich.console import Console

console = Console()

class MediaDownloader:
    """Downloads and compresses media from posts."""

    def __init__(self):
        self.media_dir = config.MEDIA_DIR
        os.makedirs(self.media_dir, exist_ok=True)
        # Convert GB to bytes
        self.max_bytes = config.MAX_MEDIA_STORAGE_GB * 1024 * 1024 * 1024
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })

    def _check_storage_limit(self) -> bool:
        """Returns True if storage usage is below limit."""
        current_bytes = storage.get_media_storage_bytes()
        if current_bytes >= self.max_bytes:
            return False
        return True

    def download_posts(self, posts: list[dict]) -> int:
        """Download media for a list of posts. Returns count of files downloaded."""
        if not self._check_storage_limit():
            console.print("[yellow]Storage limit reached, skipping downloads[/]")
            return 0
            
        count = 0
        for post in posts:
            # 1. Main media (image or video cover/file)
            if self._process_media_item(post, is_child=False):
                count += 1
                
            # 2. Carousel children
            children = post.get("carousel_children", [])
            for idx, child in enumerate(children):
                if self._process_media_item(post, is_child=True, child_item=child, index=idx):
                    count += 1
                    
            if not self._check_storage_limit():
                console.print("[yellow]Storage limit reached during batch[/]")
                break
                
        return count

    def _process_media_item(self, post: dict, is_child: bool = False, child_item: dict = None, index: int = 0) -> bool:
        """Download and save a single media item. Returns True if downloaded."""
        media_id = post["media_id"]
        
        if is_child and child_item:
            url = child_item.get("url")
            type_ = child_item.get("type", "image")
            # Use a suffix for children
            file_id = f"{media_id}_{index+1}" # start index at 1 for consistency with typical usage
            carousel_index = index
            # Inherit width/height if available, will be updated after download
            width = child_item.get("width")
            height = child_item.get("height")
            
            # If video and downloads disabled, try thumbnail
            if type_ == "video" and not config.DOWNLOAD_VIDEOS:
                if thumb := child_item.get("thumbnail_url"):
                    url = thumb
                    type_ = "image" # treat as image download
                else:
                    return False # skip if no thumbnail
        else:
            # Main post media
            if post.get("video_url"):
                if config.DOWNLOAD_VIDEOS:
                    url = post["video_url"]
                    type_ = "video"
                elif post.get("thumbnail_url") or post.get("image_url"):
                    url = post.get("thumbnail_url") or post.get("image_url")
                    type_ = "image"
                else:
                    return False
            elif post.get("image_url"):
                url = post["image_url"]
                type_ = "image"
            else:
                # No URL found
                return False
                
            file_id = media_id
            carousel_index = None
            width = post.get("media_width")
            height = post.get("media_height")

        if not url:
            return False

        ext = "jpg" if type_ == "image" else "mp4"
        filename = f"{file_id}.{ext}"
        path = os.path.join(self.media_dir, filename)

        # Skip if already exists
        if os.path.exists(path):
            return False

        try:
            resp = self._session.get(url, timeout=20)
            resp.raise_for_status()
            content = resp.content
            
            original_size = len(content)
            compressed_size = original_size
            
            if type_ == "image":
                try:
                    img = Image.open(BytesIO(content))
                    width, height = img.size
                    
                    # Resize if needed
                    if width > config.MEDIA_IMAGE_MAX_WIDTH:
                        ratio = config.MEDIA_IMAGE_MAX_WIDTH / width
                        new_height = int(height * ratio)
                        img = img.resize((config.MEDIA_IMAGE_MAX_WIDTH, new_height), Image.Resampling.LANCZOS)
                        width, height = img.size
                    
                    # Convert to RGB (remove alpha) and save as JPEG
                    if img.mode in ("RGBA", "P"):
                        img = img.convert("RGB")
                        
                    with open(path, "wb") as f:
                        img.save(f, format="JPEG", quality=85, optimize=True)
                    
                    compressed_size = os.path.getsize(path)
                except Exception as e:
                    console.print(f"[red]Image processing failed for {file_id}: {e}[/]")
                    # Fallback: save original if image processing fails (rare)
                    with open(path, "wb") as f:
                        f.write(content)
            else:
                # Video - save as is
                with open(path, "wb") as f:
                    f.write(content)

            # Update DB
            storage.save_media_files([{
                "file_id": file_id,
                "media_id": media_id,
                "file_type": type_,
                "carousel_index": carousel_index,
                "local_path": path,
                "original_url": url,
                "file_size_bytes": original_size,
                "compressed_size_bytes": compressed_size,
                "width": width,
                "height": height
            }])
            
            # If main media, update posts table
            if not is_child:
                storage.update_post_media_path(media_id, path)
                
            return True

        except Exception as e:
            # console.print(f"[dim]Download failed for {url}: {e}[/]")
            return False
