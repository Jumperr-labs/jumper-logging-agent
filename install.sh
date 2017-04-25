#!/bin/sh

# Test for root
if [ "$EUID" -ne 0 ]
  then echo "Please run as root (sudo)"
  exit
fi

# Copying the agent to its final destination
cp -r jumper-logging-agent /opt

# Install virtualenv
yes w | pip install virtualenv

# Create a virtual environment and install the app
virtualenv /opt/jumper-logging-agent/venv
source /opt/jumper-logging-agentvenv/bin/activate
python setup.py install
deactivate

# Setup the jumper agent service
cp /opt/jumper-logging-agent/jumper-agent.template /opt/jumper-logging-agent/jumper-agent.service
echo "ExecStart=/opt/jumper-logging-agent/venv/bin/python2.7 /opt/jumper-logging-agent/agent_main.py" >> /opt/jumper-logging-agent/jumper-agent.service
cp /opt/jumper-logging-agent/jumper-agent.service /lib/systemd/jumper-agent.service
ln -s /lib/systemd/jumper-agent.service /etc/systemd/system/jumper-agent.service

# Start the jumper agent service
systemctl daemon-reload
systemctl start jumper-agent.service
