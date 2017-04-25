# Test for root
if [ "$EUID" -ne 0 ]
  then echo "Please run as root (sudo)"
  exit
fi

# Copying the agent to its final destination
cp -r jumper-logging-agent /opt
cd /opt/jumper-logging-agent

# Install pip and virtualenv
apt-get install -y python-pip
yes w | pip install virtualenv

# Create a virtual environment and install the app
virtualenv venv
source venv/bin/activate
python setup.py install
deactivate

# Setup the jumper agent service
cp jumper-agent.template jumper-agent.service
echo "ExecStart=$PWD/venv/bin/python2.7 $PWD/agent_main.py" >> jumper-agent.service
cp jumper-agent.service /lib/systemd/jumper-agent.service
ln -s /lib/systemd/jumper-agent.service /etc/systemd/system/jumper-agent.service

# Start the jumper agent service
systemctl daemon-reload
systemctl start jumper-agent.service
