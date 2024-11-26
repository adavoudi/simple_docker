import os
import requests
import tarfile
import platform
import subprocess

DOCKER_REGISTRY = "https://registry-1.docker.io"

def get_auth_token(image_name):
    url = "https://auth.docker.io/token"
    params = {
        "service": "registry.docker.io",
        "scope": f"repository:{image_name}:pull"
    }
    response = requests.get(url, params=params)
    response.raise_for_status()
    return response.json()["token"]

def fetch_manifest(image_name, reference, auth_token):
    url = f"{DOCKER_REGISTRY}/v2/{image_name}/manifests/{reference}"
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Accept": ",".join([
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.docker.distribution.manifest.v2+json",
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.oci.image.manifest.v1+json"
        ])
    }
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        manifest = response.json()
        media_type = manifest.get("mediaType", response.headers.get("Content-Type"))
        return manifest, media_type
    else:
        raise Exception(f"Failed to fetch manifest: {response.status_code} {response.text}")

def download_layer(image_name, layer_digest, output_dir, auth_token):
    url = f"{DOCKER_REGISTRY}/v2/{image_name}/blobs/{layer_digest}"
    headers = {
        "Authorization": f"Bearer {auth_token}"
    }
    response = requests.get(url, headers=headers, stream=True)
    if response.status_code == 200:
        output_file = os.path.join(output_dir, layer_digest.replace("sha256:", "") + ".tar")
        with open(output_file, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        return output_file
    else:
        raise Exception(f"Failed to download layer: {response.status_code} {response.text}")

def extract_layer(layer_tar, rootfs_dir):
    with tarfile.open(layer_tar, "r") as tar:
        tar.extractall(path=rootfs_dir)

def select_platform_manifest(manifests):
    current_os = platform.system().lower()
    current_arch = platform.machine().lower()
    for manifest in manifests:
        platform_info = manifest.get('platform', {})
        os_name = platform_info.get('os', '').lower()
        architecture = platform_info.get('architecture', '').lower()
        if os_name == current_os and architecture == current_arch:
            return manifest['digest']
    # If no matching platform found, use the first one as a fallback
    print("Warning: No matching platform found. Using the first available manifest.")
    return manifests[0]['digest']

def docker_pull(image_name, tag="latest", output_dir="./rootfs"):
    auth_token = get_auth_token(image_name)
    manifest, media_type = fetch_manifest(image_name, tag, auth_token)
    
    # Check if the manifest is a list (multi-architecture)
    if media_type in (
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.index.v1+json"
    ):
        # Select the appropriate platform-specific manifest
        manifests = manifest["manifests"]
        platform_manifest_digest = select_platform_manifest(manifests)
        # Fetch the platform-specific manifest
        manifest, media_type = fetch_manifest(image_name, platform_manifest_digest, auth_token)
    
    if media_type in (
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json"
    ):
        # Proceed with downloading layers from the platform-specific manifest
        layers = manifest["layers"]
    else:
        raise Exception(f"Unsupported manifest media type: {media_type}")
    
    # Prepare directories
    os.makedirs(output_dir, exist_ok=True)
    layers_dir = os.path.join(output_dir, "layers")
    os.makedirs(layers_dir, exist_ok=True)
    
    # Download and extract each layer
    for layer in layers:
        print("Downloading layer:", layer['digest'])
        layer_digest = layer["digest"]
        layer_tar = download_layer(image_name, layer_digest, layers_dir, auth_token)
        extract_layer(layer_tar, output_dir)
    
    print(f"Image {image_name}:{tag} pulled successfully and root filesystem prepared in {output_dir}")
