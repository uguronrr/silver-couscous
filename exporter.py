import os
import sqlite3
import csv
import zipfile
import tempfile
from datetime import datetime
import config
from rich.console import Console

console = Console()

class Exporter:
    def export(self, output_path: str = None) -> str:
        """Export database tables and media directory to a portable zip file."""
        if not output_path:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = f"instagram_export_{ts}.zip"
            
        console.print(f"[dim]Exporting to {output_path}...[/]")
        
        try:
            with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
                # Export tables to CSV
                self._export_table_to_csv(zf, "posts")
                self._export_table_to_csv(zf, "comments")
                self._export_table_to_csv(zf, "media_files")
                
                # Add media files
                media_dir = config.MEDIA_DIR
                if os.path.exists(media_dir):
                    count = 0
                    for root, dirs, files in os.walk(media_dir):
                        for file in files:
                            if file.startswith("."): continue
                            file_path = os.path.join(root, file)
                            arcname = os.path.join("media", file)
                            zf.write(file_path, arcname)
                            count += 1
                    console.print(f"  Added {count} media files")
                    
            console.print(f"[green]Export complete: {output_path}[/]")
            return output_path
        except Exception as e:
            console.print(f"[red]Export failed: {e}[/]")
            if output_path and os.path.exists(output_path):
                os.unlink(output_path)
            raise
        
    def _export_table_to_csv(self, zf: zipfile.ZipFile, table: str) -> None:
        """Stream table to a temp CSV file and add to zip."""
        if not os.path.exists(config.DB_PATH):
            return

        with sqlite3.connect(config.DB_PATH) as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(f"SELECT * FROM {table}")
            except sqlite3.OperationalError:
                # Table might not exist
                return
            
            headers = [d[0] for d in cursor.description]
            
            with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".csv", encoding="utf-8", newline="") as tf:
                writer = csv.writer(tf)
                writer.writerow(headers)
                
                row_count = 0
                while True:
                    rows = cursor.fetchmany(1000)
                    if not rows:
                        break
                    writer.writerows(rows)
                    row_count += 1000
                
                tf_path = tf.name
                
        zf.write(tf_path, f"{table}.csv")
        os.unlink(tf_path)
        console.print(f"  Exported ~{row_count} rows from {table}")
