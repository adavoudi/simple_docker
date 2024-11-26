import os
import sys
import subprocess
import ctypes
import signal

# Module-level variables
CGROUP_PATH = None
VETH_HOST = None
VETH_CONTAINER = None

def set_hostname(hostname):
    libc = ctypes.CDLL(None)
    hostname_bytes = hostname.encode('utf-8')
    result = libc.sethostname(hostname_bytes, len(hostname_bytes))
    if result != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))

def grandchild_process(rootfs_path):
    # Grandchild process (C2) - acts as init process in PID namespace
    set_hostname("simple-container")
    
    # Make mount propagation private
    subprocess.run(['mount', '--make-rprivate', '/'], check=True)
    
    setup_filesystem(rootfs_path)
    mount_proc()
    
    # Fork to create the shell as a child process
    pid = os.fork()
    if pid == 0:
        # Child process (the shell)
        os.execv("/bin/sh", ["/bin/sh"])
    else:
        # Init process
        # Wait for the shell process to exit
        os.waitpid(pid, 0)
        # Clean up
        subprocess.run(['umount', '/proc'], check=True)
        os._exit(0)

def start_container(rootfs_path):
    print("Starting container...")

    # Set the parent process into its own process group
    os.setpgrp()

    # Ignore SIGINT in the parent process
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    # Create a pipe for communication between parent and child
    parent_pipe, child_pipe = os.pipe()

    pid = os.fork()

    if pid == 0:
        # Child process (C1)
        os.close(parent_pipe)  # Close unused end in child

        # Unshare the required namespaces
        os.unshare(os.CLONE_NEWUTS | os.CLONE_NEWNS | os.CLONE_NEWNET | os.CLONE_NEWPID)

        # Fork again to make the grandchild process
        pid2 = os.fork()
        if pid2 == 0:
            # Grandchild process (C2)
            grandchild_process(rootfs_path)
        else:
            # Intermediate child (C1)
            # Write the grandchild PID to the parent
            os.write(child_pipe, str(pid2).encode())
            os.close(child_pipe)

            # Wait for the grandchild to exit
            os.waitpid(pid2, 0)

            # Exit after the grandchild has exited
            os._exit(0)
    else:
        # Parent process (P)
        os.close(child_pipe)  # Close unused end in parent

        # Read the grandchild PID from the pipe
        grandchild_pid_bytes = os.read(parent_pipe, 1024)
        os.close(parent_pipe)
        grandchild_pid = int(grandchild_pid_bytes.decode().strip())

        print(f"Container process started with PID {grandchild_pid}")

        try:
            enable_ip_forwarding()
            limit_resources(grandchild_pid)
            setup_network(grandchild_pid)
            setup_nat()
            os.waitpid(pid, 0)  # Wait for intermediate child (C1)
        finally:
            cleanup()
            print("Container exited and resources cleaned up.")
        
def setup_filesystem(rootfs_path):
    """Set up the root filesystem for the container."""
    if not os.path.exists(rootfs_path):
        raise FileNotFoundError("Root filesystem not found at " + rootfs_path)
    os.chroot(rootfs_path)
    os.chdir("/")
    print(f"Root filesystem changed to {rootfs_path}")

def mount_proc():
    """Mount /proc inside the container."""
    if not os.path.exists("/proc"):
        os.mkdir("/proc")
    subprocess.run(["mount", "-t", "proc", "proc", "/proc"], check=True)
    print("/proc mounted")

def limit_resources(container_pid):
    """Limit resources for the container process using cgroups v2."""
    global CGROUP_PATH
    cgroup_base = "/sys/fs/cgroup"
    cgroup_path = os.path.join(cgroup_base, "mycontainer")
    os.makedirs(cgroup_path, exist_ok=True)

    # Limit CPU to 10% of a single core
    with open(f"{cgroup_path}/cpu.max", "w") as f:
        f.write("10000 100000")

    # Add the container process to the cgroup
    with open(f"{cgroup_path}/cgroup.procs", "w") as f:
        f.write(str(container_pid))

    # Store the cgroup path for cleanup
    CGROUP_PATH = cgroup_path

    print(f"Resources limited for PID {container_pid}")

def setup_network(container_pid):
    """Set up networking for the container process."""
    global VETH_HOST, VETH_CONTAINER
    VETH_HOST = "veth0"
    VETH_CONTAINER = "veth1"

    # Create a pair of virtual ethernet devices
    subprocess.run(["ip", "link", "add", VETH_HOST, "type", "veth", "peer", "name", VETH_CONTAINER], check=True)

    # Move one end into the container's network namespace
    subprocess.run(["ip", "link", "set", VETH_CONTAINER, "netns", str(container_pid)], check=True)

    # Configure the host side interface
    subprocess.run(["ip", "addr", "add", "192.168.1.1/24", "dev", VETH_HOST], check=True)
    subprocess.run(["ip", "link", "set", VETH_HOST, "up"], check=True)

    # Configure the container side interface
    subprocess.run([
        "nsenter",
        f"--net=/proc/{container_pid}/ns/net",
        "ip", "addr", "add", "192.168.1.2/24", "dev", VETH_CONTAINER
    ], check=True)
    subprocess.run([
        "nsenter",
        f"--net=/proc/{container_pid}/ns/net",
        "ip", "link", "set", VETH_CONTAINER, "up"
    ], check=True)
    subprocess.run([
        "nsenter",
        f"--net=/proc/{container_pid}/ns/net",
        "ip", "route", "add", "default", "via", "192.168.1.1"
    ], check=True)

    print(f"Network setup for PID {container_pid}")

def enable_ip_forwarding():
    """Enable IP forwarding on the host."""
    subprocess.run(["sysctl", "-w", "net.ipv4.ip_forward=1"], check=True)
    print("IP forwarding enabled.")

def setup_nat():
    """Set up NAT for the container network."""
    global VETH_HOST
    # Define the container subnet
    container_subnet = "192.168.1.0/24"
    # Add iptables rule for NAT
    subprocess.run([
        "iptables", "-t", "nat", "-A", "POSTROUTING",
        "-s", container_subnet, "!", "-o", VETH_HOST, "-j", "MASQUERADE"
    ], check=True)
    # Allow forwarding
    subprocess.run(["iptables", "-A", "FORWARD", "-i", VETH_HOST, "-j", "ACCEPT"], check=True)
    subprocess.run(["iptables", "-A", "FORWARD", "-o", VETH_HOST, "-j", "ACCEPT"], check=True)
    print("NAT and forwarding rules set up.")

def cleanup():
    """Clean up resources after the container exits."""
    print("Cleaning up resources...")

    # Clean up cgroups
    cleanup_cgroups()

    # Clean up network interfaces
    cleanup_network()

    cleanup_nat()

def cleanup_nat():
    """Clean up NAT rules."""
    global VETH_HOST
    container_subnet = "192.168.1.0/24"
    subprocess.run([
        "iptables", "-t", "nat", "-D", "POSTROUTING",
        "-s", container_subnet, "!", "-o", VETH_HOST, "-j", "MASQUERADE"
    ], check=True)
    subprocess.run(["iptables", "-D", "FORWARD", "-i", VETH_HOST, "-j", "ACCEPT"], check=True)
    subprocess.run(["iptables", "-D", "FORWARD", "-o", VETH_HOST, "-j", "ACCEPT"], check=True)
    print("NAT and forwarding rules cleaned up.")

def cleanup_cgroups():
    """Remove the cgroup created for the container."""
    global CGROUP_PATH
    if CGROUP_PATH:
        try:
            # Remove the cgroup directory
            os.rmdir(CGROUP_PATH)
            print(f"Cgroup {CGROUP_PATH} removed.")
        except Exception as e:
            print(f"Failed to remove cgroup {CGROUP_PATH}: {e}")

def cleanup_network():
    """Remove the virtual ethernet interfaces."""
    global VETH_HOST
    if VETH_HOST:
        # Delete the host side veth interface
        subprocess.run(["ip", "link", "delete", VETH_HOST], check=True)
        print(f"Network interface {VETH_HOST} deleted.")
