import os
import zipfile
import sys
from pathlib import Path

def create_dashboard_zip():
    repo_root = Path(r'c:\Users\1039081\Downloads\PE_Dashboard')
    output_zip_in_repo = repo_root / 'PE_Dashboard.zip'
    output_zip_in_downloads = repo_root.parent / 'PE_Dashboard_Package.zip'

    # Patterns / dirs / files to exclude
    exclude_dirs = {
        '.git',
        'node_modules',
        '.venv',
        '__pycache__',
        '.claude',
        'tmp',
        'graphify-out',
        '.system_generated',
        '.vscode',
        'dist',
        '.pytest_cache'
    }

    exclude_files = {
        'PE_Dashboard.zip',
        'PE_Dashboard_Package.zip',
        'PE_Dashboard_MFE_Portal_Build.zip',
        'diff_output.txt',
        '.server_pid',
        '.pe_baseline.db-shm',
        '.pe_baseline.db-wal',
        'server_stdout.txt',
        'server_stderr.txt',
        'server_err.txt',
        'debug.log'
    }

    print(f"Creating zip from: {repo_root}")
    count = 0
    total_uncompressed_bytes = 0

    with zipfile.ZipFile(output_zip_in_repo, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(repo_root):
            # Filter directories in-place
            dirs[:] = [d for d in dirs if d not in exclude_dirs and not d.startswith('.git')]

            for f in files:
                if f in exclude_files or f.endswith('.pyc') or f.endswith('.pyo'):
                    continue

                full_path = Path(root) / f
                rel_path = full_path.relative_to(repo_root)

                # Ensure root start.bat is at the top of the archive
                zf.write(full_path, arcname=str(rel_path))
                count += 1
                total_uncompressed_bytes += full_path.stat().st_size

    # Also make a copy in Downloads folder for easy access
    import shutil
    shutil.copy2(output_zip_in_repo, output_zip_in_downloads)

    zip_size_mb = output_zip_in_repo.stat().st_size / (1024 * 1024)
    raw_size_mb = total_uncompressed_bytes / (1024 * 1024)

    print(f"Zip created successfully!")
    print(f"Total files: {count}")
    print(f"Uncompressed size: {raw_size_mb:.2f} MB")
    print(f"Compressed zip size: {zip_size_mb:.2f} MB")
    print(f"Output 1: {output_zip_in_repo}")
    print(f"Output 2: {output_zip_in_downloads}")

if __name__ == '__main__':
    create_dashboard_zip()
