#!/bin/bash
# Licensed under the MIT License. You may obtain a copy of the License at:
#   https://opensource.org/licenses/MIT
#
# Author: cemaxecuter
#
# This script supports two actions: "stop" and "start".
#
# The "stop" action does the following:
#   - It creates a temporary shell script that, on the remote host, checks for the
#     following processes (by matching a fixed part of their command lines):
#         1. Any process whose command line contains "/etc/init.d/S55drone"
#         2. Any process whose command line contains "/usr/sbin/droneangle.sh"
#         3. Any process whose command line contains "/usr/sbin/done_dji_release"
#   - If any one of these is found, it kills all instances (using SIGKILL) in one pass.
#
# The "start" action runs the remote init script in a detached fashion.
#
# Usage:
#   ./drone_control.sh stop   # Stops the service (by killing its processes)
#   ./drone_control.sh start  # Starts the service using the remote init script

# Remote host details and plaintext password
HOST="172.31.100.2"
USER="root"
PASSWORD="abawavearm"

# Validate input parameter
if [ $# -ne 1 ]; then
    echo "Usage: $0 [start|stop]"
    exit 1
fi

ACTION="$1"

# SSH options: bypass host key checking and do not update the known_hosts file.
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
VERBOSE="${FPV_DJI_GUARD_VERBOSE:-0}"

log() {
  if [ "$VERBOSE" = "1" ]; then
    echo "$@"
  fi
}

# Function: Stop the service by killing one or more target processes.
stop_service() {
  # Create a temporary remote script that will kill the service processes.
  LOCAL_TMP_SCRIPT=$(mktemp /tmp/remote_kill.XXXXXX.sh)

  cat << 'EOF' > "$LOCAL_TMP_SCRIPT"
#!/bin/sh
echo "Starting remote kill script..."

# Define the fixed strings (patterns) to search for.
# We only need to catch one to determine that the service is running.
TARGETS="/etc/init.d/S55drone
/usr/sbin/droneangle.sh
/usr/sbin/done_dji_release"

found=0
# For each target pattern, look for any matching process.
for target in $TARGETS; do
    echo "Checking for processes matching: $target"
    pids=$(ps auxx | grep -F "$target" | grep -v grep | awk '{print $1}')
    if [ -n "$pids" ]; then
        echo "Found process(es) for [$target]: $pids"
        found=1
        # Kill all PIDs found for this target.
        for pid in $pids; do
            echo "Killing PID $pid..."
            kill -9 "$pid" 2>/dev/null
        done
    else
        echo "No processes found for [$target]."
    fi
done

if [ "$found" -eq 1 ]; then
    echo "At least one target process was found and kill commands issued."
else
    echo "No target processes were running."
fi

echo "Final process list (filtered):"
ps auxx | grep -E "S55drone|droneangle|done_dji_release" | grep -v grep
EOF

  # Make the temporary script executable.
  chmod +x "$LOCAL_TMP_SCRIPT"

  log "Copying kill script to remote host..."
  if [ "$VERBOSE" = "1" ]; then
    sshpass -p "$PASSWORD" scp -O $SSH_OPTS "$LOCAL_TMP_SCRIPT" "$USER@$HOST:/tmp/remote_kill.sh"
  else
    sshpass -p "$PASSWORD" scp -O -q $SSH_OPTS "$LOCAL_TMP_SCRIPT" "$USER@$HOST:/tmp/remote_kill.sh" >/dev/null 2>&1
  fi

  log "Executing remote kill script..."
  if [ "$VERBOSE" = "1" ]; then
    sshpass -p "$PASSWORD" ssh -tt $SSH_OPTS "$USER@$HOST" "sh /tmp/remote_kill.sh; rm /tmp/remote_kill.sh"
  else
    sshpass -p "$PASSWORD" ssh -tt -q $SSH_OPTS "$USER@$HOST" "sh /tmp/remote_kill.sh; rm /tmp/remote_kill.sh" >/dev/null 2>&1
  fi

  # Clean up the local temporary file.
  rm "$LOCAL_TMP_SCRIPT"

  log "Remote kill script executed."
}

# Function: Start the service using the remote init script, detached from the session.
start_service() {
  log "Starting Drone Daemon on remote host..."
  # Use nohup, background the command, and redirect output so the SSH session can disconnect.
  if [ "$VERBOSE" = "1" ]; then
    sshpass -p "$PASSWORD" ssh $SSH_OPTS "$USER@$HOST" "nohup /etc/init.d/S55drone start > /dev/null 2>&1 &"
  else
    sshpass -p "$PASSWORD" ssh -q $SSH_OPTS "$USER@$HOST" "nohup /etc/init.d/S55drone start > /dev/null 2>&1 &" >/dev/null 2>&1
  fi
}

# Main action selection.
case "$ACTION" in
  stop)
    log "Executing stop command..."
    stop_service
    ;;
  start)
    log "Executing start command..."
    start_service
    ;;
  *)
    echo "Invalid option: $ACTION"
    echo "Usage: $0 [start|stop]"
    exit 1
    ;;
esac
