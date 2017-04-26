#!/bin/bash

# Test for root
if [[ "$EUID" -ne 0 ]]; then
  echo "Please run as root (sudo).  Aborting." >&2
  exit 1
fi

OS="debian"

if [ -f /etc/lsb-release ]; then
    OS=$(awk '/DISTRIB_ID=/' /etc/*-release | sed 's/DISTRIB_ID=//' | tr '[:upper:]' '[:lower:]')
fi

if [ $OS = "ubuntu" ]; then
    [[ `initctl` =~ -\.mount ]] || ( echo "init.d is required but it's not running.  Aborting." >&2; exit 1 )    
else
    # Check for systemd
    [[ `systemctl` =~ -\.mount ]] || ( echo "systemd is required but it's not running.  Aborting." >&2; exit 1 )
fi

# Check for python2.7
command -v python2.7 >/dev/null 2>&1 || { echo "python2.7 is required but it's not installed.  Aborting." >&2; exit 1; }

# Check for pip
command -v pip >/dev/null 2>&1 || { echo "pip is required but it's not installed.  Aborting." >&2; exit 1; }

set -e

INSTALLATION_LOG=/tmp/jumper_agent_installation.log
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
DEST_DIR=/opt/jumper_logging_agent
FIFO_DIR=/var/run/jumper_logging_agent
SERVICE_USER=jumperagent
SERVICE_NAME=jumper-agent

if ! command -v virtualenv --version >/dev/null 2>&1; then
    echo Installing virtualenv...
    yes w | python2.7 -m pip -qq install virtualenv
fi

if id -u ${SERVICE_USER} >/dev/null 2>&1; then
    echo Reusing user ${SERVICE_USER}
else
    useradd ${SERVICE_USER} -M -s /usr/sbin/nologin -c "Jumper Logging Agent"
fi

echo Creating directories...
rm -rf ${DEST_DIR}
mkdir -p ${DEST_DIR}
mkdir -p ${FIFO_DIR}
chown ${SERVICE_USER}:${SERVICE_USER} ${FIFO_DIR}

# Copying the agent to its final destination
COPY_FILES="jumper_logging_agent README.rst setup.py setup.cfg agent_main.py"
for FILE in ${COPY_FILES}; do
    cp -R ${SCRIPT_DIR}/${FILE} ${DEST_DIR}/
done

chown -R ${SERVICE_USER}:${SERVICE_USER} ${DEST_DIR}
chmod -R u+rw,g+rw ${DEST_DIR}

su -s /bin/bash ${SERVICE_USER} <<EOFSU
cd ${DEST_DIR}

# Create a virtual environment
virtualenv -p python2.7 ./venv >/dev/null
source ./venv/bin/activate

# Install dependent python packages
if ! python2.7 setup.py -q install >${INSTALLATION_LOG} 2>&1; then
    echo Installation of dependent python packages failed. See ${INSTALLATION_LOG} for details. >&2
    exit 1
fi

EOFSU

# Setup the jumper agent service
echo Setting up service ${SERVICE_NAME}...

if [ $OS = "ubuntu" ]; then
    SERVICE_FILE=/etc/init.d/${SERVICE_NAME}

    cp ${SCRIPT_DIR}/jumper.template ${SERVICE_FILE}

    chmod 755 ${SERVICE_FILE}

    # Start the jumper agent service
    update-rc.d ${SERVICE_NAME} defaults
    update-rc.d ${SERVICE_NAME} enable
    service ${SERVICE_NAME} start

    sleep 1

    if [[ "`service ${SERVICE_NAME} status`" -ne "Running" ]]; then
        echo "Error: Service ${SERVICE_NAME} is not running. Status information: " >&2
        exit 1
    fi
else
    SERVICE_FILE=/lib/systemd/${SERVICE_NAME}.service

    cp ${SCRIPT_DIR}/jumper-agent.template ${SERVICE_FILE}
    echo "ExecStart=${DEST_DIR}/venv/bin/python2.7 ${DEST_DIR}/agent_main.py" >> ${SERVICE_FILE}
    echo "User=${SERVICE_USER}" >> ${SERVICE_FILE}
    ln -fs ${SERVICE_FILE} /etc/systemd/system/${SERVICE_NAME}.service

    # Start the jumper agent service
    systemctl daemon-reload
    systemctl start jumper-agent.service

    sleep 1

    if [[ "`systemctl is-active ${SERVICE_NAME}`" -ne "active" ]]; then
        echo "Error: Service ${SERVICE_NAME} is not running. Status information: " >&2
        echo "" >&2
        systemctl status ${SERVICE_NAME} >&2
        exit 1
    fi
fi


echo Success! Jumper logging agent is now installed and running.