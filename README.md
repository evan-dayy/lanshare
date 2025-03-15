# LAN Sharing Service
A local-area-network (LAN) sharing service that shares files and clipboards across different devices in local area network, essientially, it means transferring files directly between devices on the same network without going through the internet. 

### CUJ
---
- *CUJ#1:* sub LAN with access code;
- *CUJ#2:* peer discoveries (in LAN and sub-LAN);
- *CUJ#3:* access level (secured mode, admin, visitor, ...);
- *CUJ#4:* messages transmission & history (text only);
- *CUJ#5:* file transmission (different format);
- *CUJ#6:* streaming across LAN;
- *CUJ#7:* backup and restore;

### Prerequisite
---
First, make a new folder and clone the repo:
```sh
mkdir lanss && cd lanss
git clone git@github.com:amyhuang95/LAN-Sharing-Service.git
cd LAN-Sharing-Service
```

download all python dependencies:

```
pip install -r requirements.txt
```
**Notes: Make sure all the device are in the same LAN to discover your peers.**

### Create a User with `username`
---
Create a user with `username = evan-dayy`. Add --share_clipboard or -sc flag to activate clipboard sharing feature.
```sh
python create.py create --username <USERNAME> [-sc]
```

Access to the LAN Terminal command;
```

Welcome to LAN Share, evan-dayy#81b6!
Type 'help' for available commands
evan-dayy#81b6@LAN(192.168.4.141)# help

Available commands:
  ul     - List online users
  msg    - Send a message (msg <username>)
  lm     - List all messages
  om     - Open a message conversation (om <conversation_id>)
  share  - Share a file or directory (share <path>)
  files  - List shared files
  access - Manage access to shared resources (access <id> <user> [add|rm])
  all    - Share resource with everyone (all <id> [on|off])
  sc     - Share clipboard (sc <username_1> <username_2> ...)
  rc     - Receive clipboard from peers (rc <username_1> <username_2> ...)
  debug  - Toggle debug mode
  clear  - Clear screen
  help   - Show this help message
  exit   - Exit the session
evan-dayy#81b6@LAN(192.168.4.141)#

```

#### Note
* To enable two way exchange of clipboard content, peers needs to add each other to their sending and receiving lists with `sc` and `rc` commands. For example, if Peer1 and Peer2 want to exchange clipboard data:

  Peer1 needs to run:
  ```
  sc Peer2
  rc Peer2
  ```

  Peer2 needs to run:
  ```
  sc Peer1
  rc Peer1
  ```
  With this setup, when Peer1 copies some text, Peer2 will be able to paste right away. Same for the opposite direction. 

### Demo
---
#### Video (iter1) 

https://github.com/user-attachments/assets/549d37ee-d8e8-4d7f-b6bb-4cc80f819626


#### Video (iter2) 


https://github.com/user-attachments/assets/3b515191-a86d-436f-904b-736dbc586298


