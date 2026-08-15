#!/usr/bin/env python3
import os
import re
import sys
import json
import shutil
import tempfile
import xml.etree.ElementTree as ET
import urllib.request
import subprocess

# Configuration
LIST_FILE = "appimagelist.md"
DATABASE_DIR = "database"
API_DIR = os.path.join(DATABASE_DIR, "api", "v1")
APPS_API_DIR = os.path.join(API_DIR, "apps")
ICONS_DIR = os.path.join(DATABASE_DIR, "icons")

def setup_directories():
    for d in [DATABASE_DIR, API_DIR, APPS_API_DIR, ICONS_DIR]:
        os.makedirs(d, exist_ok=True)

def parse_app_list(filepath):
    print(f"Parsing {filepath}...")
    repos = []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                # Capture domain, owner, and repo from markdown links
                match = re.search(r'https://([^/]+)/([^/]+)/([^/)\]]+)', line)
                if match:
                    domain = match.group(1)
                    owner = match.group(2)
                    repo = match.group(3).replace(".git", "")
                    repos.append({"domain": domain, "owner": owner, "repo": repo})
    except Exception as e:
        print(f"Error reading list: {e}")
    return repos

def get_latest_release(repo_info):
    domain = repo_info["domain"]
    owner = repo_info["owner"]
    repo = repo_info["repo"]
    
    if domain == "github.com":
        url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    else:
        # Assuming Gitea / Forgejo API which is compatible with GitHub's
        url = f"https://{domain}/api/v1/repos/{owner}/{repo}/releases/latest"
        
    req = urllib.request.Request(url)
    
    if domain == "github.com":
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            req.add_header("Authorization", f"token {token}")
            
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        return None

def download_file(url, dest):
    print(f"Downloading {url}...")
    try:
        urllib.request.urlretrieve(url, dest)
        return True
    except Exception:
        return False

def extract_appimage(appimage_path, extract_dir):
    os.chmod(appimage_path, 0o755)
    cwd = os.getcwd()
    os.chdir(extract_dir)
    try:
        subprocess.run([appimage_path, "--appimage-extract"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.chdir(cwd)
        # Squashfs-root is created in extract_dir
        # It could be a directory or a symlink. We need the actual path it resolves to.
        squashfs_root = os.path.join(extract_dir, "squashfs-root")
        if os.path.islink(squashfs_root):
            return os.path.join(extract_dir, os.readlink(squashfs_root))
        return squashfs_root
    except Exception as e:
        os.chdir(cwd)
        print(f"Extraction failed: {e}")
        return None

def parse_desktop_file(filepath):
    data = {"id": os.path.basename(filepath).replace(".desktop", "")}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line.startswith("Name="):
                    data["name"] = line[5:]
                elif line.startswith("Comment="):
                    data["summary"] = line[8:]
                elif line.startswith("Icon="):
                    data["icon_name"] = line[5:]
                elif line.startswith("Categories="):
                    data["categories"] = [c for c in line[11:].split(";") if c]
    except Exception:
        pass
    return data

def find_metadata(squashfs_root):
    app_data = None
    
    # Try AppStream first
    for d in [os.path.join(squashfs_root, "usr", "share", "metainfo"), os.path.join(squashfs_root, "usr", "share", "appdata")]:
        if os.path.isdir(d):
            for f in os.listdir(d):
                if f.endswith(('.xml', '.appdata.xml', '.metainfo.xml')):
                    try:
                        tree = ET.parse(os.path.join(d, f))
                        root = tree.getroot()
                        app_data = {
                            "id": root.findtext('id') or "unknown",
                            "name": root.findtext('name') or "Unknown",
                            "summary": root.findtext('summary') or "",
                            "description": "".join(root.find('description').itertext()).strip() if root.find('description') is not None else ""
                        }
                        break
                    except Exception:
                        pass
        if app_data: break

    # If no AppStream, fallback to .desktop file
    if not app_data:
        desktop_files = [f for f in os.listdir(squashfs_root) if f.endswith('.desktop')]
        if not desktop_files and os.path.isdir(os.path.join(squashfs_root, "usr", "share", "applications")):
            app_dir = os.path.join(squashfs_root, "usr", "share", "applications")
            desktop_files = [os.path.join("usr", "share", "applications", f) for f in os.listdir(app_dir) if f.endswith('.desktop')]
            
        if desktop_files:
            desktop_path = os.path.join(squashfs_root, desktop_files[0])
            print(f"Fallback to desktop file: {desktop_path}")
            app_data = parse_desktop_file(desktop_path)
            if "name" not in app_data:
                app_data["name"] = app_data["id"]

    return app_data

def copy_icon(squashfs_root, app_id, icon_name, extract_dir):
    dir_icon = os.path.join(squashfs_root, ".DirIcon")
    
    # If .DirIcon doesn't exist or is empty, try looking for a png/svg matching the icon_name
    if not os.path.exists(dir_icon) or os.path.getsize(dir_icon) == 0:
        found_icon = None
        if icon_name:
            for root_dir, _, files in os.walk(squashfs_root):
                for f in files:
                    if (f == f"{icon_name}.png" or f == f"{icon_name}.svg") and os.path.getsize(os.path.join(root_dir, f)) > 0:
                        found_icon = os.path.join(root_dir, f)
                        break
                if found_icon: break
        
        # Fallback to any root png if no name match
        if not found_icon:
            for f in os.listdir(squashfs_root):
                if (f.endswith('.png') or f.endswith('.svg')) and os.path.getsize(os.path.join(squashfs_root, f)) > 0:
                    found_icon = os.path.join(squashfs_root, f)
                    break
                    
        if found_icon:
            dir_icon = found_icon
        else:
            return None

    if os.path.exists(dir_icon) and os.path.getsize(dir_icon) > 0:
        ext = ".png"
        try:
            out = subprocess.check_output(["file", "-b", "--mime-type", dir_icon]).decode().strip()
            if "svg" in out: ext = ".svg"
        except: pass
        dest = os.path.join(ICONS_DIR, f"{app_id}{ext}")
        shutil.copy2(dir_icon, dest)
        return f"/database/icons/{app_id}{ext}"
    return None

def main():
    setup_directories()
    repos = parse_app_list(LIST_FILE)
    
    repos_to_process = repos
    all_apps = []
    
    for repo_info in repos_to_process:
        owner_repo = f"{repo_info['owner']}/{repo_info['repo']}"
        print(f"\n--- Processing {owner_repo} ---")
        release_info = get_latest_release(repo_info)
        if not release_info:
            print("No release info found.")
            continue
            
        assets = release_info.get("assets", [])
        appimage_asset = next((a for a in assets if a["name"].endswith(".AppImage") and "x86_64" in a["name"].lower()), None)
        if not appimage_asset:
            appimage_asset = next((a for a in assets if a["name"].endswith(".AppImage") and "aarch64" not in a["name"].lower() and "arm" not in a["name"].lower()), None)
            
        if not appimage_asset:
            print(f"No valid AppImage found for {owner_repo}")
            continue
            
        download_url = appimage_asset["browser_download_url"]
        
        with tempfile.TemporaryDirectory() as temp_dir:
            appimage_path = os.path.join(temp_dir, appimage_asset["name"])
            if not download_file(download_url, appimage_path): continue
                
            squashfs_root = extract_appimage(appimage_path, temp_dir)
            if not squashfs_root: continue
                
            app_data = find_metadata(squashfs_root)
            if app_data:
                app_data["repo"] = owner_repo
                app_data["version"] = release_info.get("tag_name", "unknown")
                app_data["download_url"] = download_url
                
                # Try to extract icon
                icon_path = copy_icon(squashfs_root, app_data["id"], app_data.get("icon_name", ""), temp_dir)
                if icon_path:
                    app_data["icon_url"] = icon_path
                
                json_path = os.path.join(APPS_API_DIR, f"{app_data['id']}.json")
                with open(json_path, 'w', encoding='utf-8') as f:
                    json.dump(app_data, f, indent=2)
                    
                all_apps.append({
                    "id": app_data["id"],
                    "name": app_data.get("name", "Unknown"),
                    "summary": app_data.get("summary", ""),
                    "icon_url": icon_path
                })
                print(f"Successfully processed {app_data['name']}")
            else:
                print("Could not find any metadata.")

    with open(os.path.join(API_DIR, "apps.json"), 'w', encoding='utf-8') as f:
        json.dump(all_apps, f, indent=2)
    print(f"\n--- Done: Processed {len(all_apps)} apps ---")

if __name__ == "__main__":
    main()
