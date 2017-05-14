# Jumper Logging Agent
This program is part of Jumper Insights, a full visibility platform for IoT systems. Visit https://www.jumper.io/insights/ to learn more.

The logging agent is a background service that allows Linux- and Windows-based systems to send logs to Jumper's backend.

## Prerequesites
- You need to have pip installed in order for the installation to work. If you don't have it yet, type the following command:

    `sudo apt-get install python-pip`

## Installation
The following installation manual was tested on Unbuntu, Debian and Raspbian.

- Clone the code from github or download and extract with the following command:

    `wget -qO- https://github.com/Jumperr-labs/jumper-logging-agent/archive/0.0.1.tar.gz | tar xvz`

- Run the following command to complete the installation and fire up the agent service:

	`sudo sh jumper-logging-agent/install.sh`

## Configuration
Save your configuration file here: `/etc/jumper_logging_agent/config.json`

The _"config.json"_ file should be in a JSON format and ahve the following objects:
```json
{
    "write_key": "TOP_SECRET",
    "project_id": "PROJECT_AWESOME"
}
```

## Usage
Start the service:
`sudo service jumper-agent start`

Stop the service:
`sudo service jumper-agent stop`

Check if the agent is running:
`sudo service jumper-agent status`
