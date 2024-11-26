import argparse
import sys
import os

from docker_pull import docker_pull
from utils import decode_image
from container_runtime import start_container

def main():
    parser = argparse.ArgumentParser(description='Simple Docker-like application')
    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    # Pull command
    pull_parser = subparsers.add_parser('pull', help='Pull a Docker image')
    pull_parser.add_argument('image', help='Image name in the format image_name:tag')

    # Run command
    run_parser = subparsers.add_parser('run', help='Run a container')
    run_parser.add_argument('image', help='Image name in the format image_name:tag')

    args = parser.parse_args()

    if args.command == 'pull':
        image_name, tag = decode_image(args.image)
        rootfs_path = f'./images/{image_name}/{tag}'
        print(f"Pulling image {image_name}:{tag}...")
        docker_pull(image_name, tag, output_dir=rootfs_path)
    elif args.command == 'run':
        image_name, tag = decode_image(args.image)
        rootfs_path = f'./images/{image_name}/{tag}'
        if os.geteuid() != 0:
            print("The 'run' command must be run as root!")
            sys.exit(1)
        start_container(rootfs_path)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
