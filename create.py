import argparse
from lanshare.config.settings import Config
from lanshare.core.udp_discovery import UDPPeerDiscovery
from lanshare.ui.session import InteractiveSession

def main():
    parser = argparse.ArgumentParser(description='LAN Peer Discovery Service')
    parser.add_argument('command', choices=['create'], help='Command to execute')
    parser.add_argument('--username', help='Username for the peer', required=False)
    args = parser.parse_args()

    if args.command == 'create':
        if not args.username:
            parser.error("Username is required for 'create' command")
        
        config = Config()
        discovery = UDPPeerDiscovery(args.username, config)
        discovery.start()
        session = InteractiveSession(discovery)
        session.start()

if __name__ == "__main__":
    main()