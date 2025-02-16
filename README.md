# LAN Sharing Service
A local-area-network (LAN) sharing service that shares files and clipboards across different devices in local area network, essientially, it means transferring files directly between devices on the same network without going through the internet. 

### Prerequisite
---
First, make a new folder and clone the repo:
```sh
mkdir lanss && cd lanss
git clone git@github.com:amyhuang95/P2P-Data-Sharing-App.git
cd P2P-Data-Sharing-App
```

download all python dependencies:

```
pip install -r requirements.txt
```
* Notes: Make sure all the device are in the same LAN to discover your peers.

### Create User
```sh
python create.py create --username evan-dayy
```

Access to the LAN Terminal command;
```
Welcome to LAN Share, evan-dayy!
Type 'help' for available commands
evan-dayy@LAN(192.168.4.141)# help

Available commands:
  ul     - List online users
  debug  - Toggle debug mode
  clear  - Clear screen
  help   - Show this help message
  exit   - Exit the session
evan-dayy@LAN(192.168.4.141)#
```