Jumper Logging Agent
====================

This is a background service that allows Linux- and Windows-based systems to send logs to the Jumper backend.

Installation
------------

Clone code from github or download and extract with the following command:

```
my-app/
  README.md
  node_modules/
  package.json
  public/
    index.html
    favicon.ico
  src/
    App.css
    App.js
    App.test.js
    index.css
    index.js
    logo.svg
```
wget -qO- https://s3-us-west-1.amazonaws.com/jumper-agent/jumper-logging-agent.tar.gz | tar xvz


Run the following command to complete the installation and fire up the agent service:

sudo bash jumper-logging-agent/install.sh

