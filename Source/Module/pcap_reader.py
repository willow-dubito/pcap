"""
Module pcap_reader
"""
import logging
import sys
import memory
from netaddr import IPAddress
import threading
import base64
import malicious_traffic_identifier
import communication_details_fetch

# Feature toggle
tls_view_feature = False

class PcapEngine():
    """
    PcapEngine: To support different pcap parser backend engine to operate reading pcap
    Current Support:
        * Scapy
        * Pyshark
    """

    def __init__(self, pcap_file_name, pcap_parser_engine="scapy"):
        """
        Init function imports libraries based on the parser engine selected
        Return:
        * packetDB ==> Full Duplex Packet Streams
          - Used while finally plotting streams as graph
          - dump packets during report generation
        * lan_hosts ==> Private IP (LAN) list
          - device details
        * destination_hosts ==> Destination Hosts
          - communication details
          - tor identification
          - malicious identification
        """

        # Initialize Data Structures
        memory.packet_db = {}
        memory.lan_hosts = {}
        memory.destination_hosts = {}
        memory.possible_mal_traffic = []
        memory.possible_tor_traffic = []

        # Set Pcap Engine
        self.engine = pcap_parser_engine

        # Import library for pcap parser engine selected
        if pcap_parser_engine == "scapy":
            try:
                from scapy.all import rdpcap
            except:
                logging.error("Cannot import selected pcap engine: Scapy!")
                sys.exit()
            
            try:
                from scapy.all import load_layer
                global tls_view_feature
                tls_view_feature = True
                logging.info("tls view feature enabled")
            except:
                logging.info("tls view feature not enabled")
            
            if tls_view_feature:
                load_layer("tls")

            # Scapy sessions and other types use more O(N) iterations so just
            # - use rdpcap + our own iteration (create full duplex streams)
            self.packets = rdpcap(pcap_file_name)
        
        elif pcap_parser_engine == "pyshark":
            try:
                import pyshark
            except:
                logging.error("Cannot import selected pcap engine: PyShark!")
                sys.exit()
            self.packets = pyshark.FileCapture(pcap_file_name, include_raw=True, use_json=True)
            #self.packets.load_packets()
            #self.packets.apply_on_packets(self.analyse_packet_data, timeout=100)

        # Analyse capture to populate data
        self.analyse_packet_data()

        # <TODO>: Add other pcap engine modules to generate packetDB

    #@retry(tries=5, errors=memory.CouldNotLock)
    def analyse_packet_data(self):
            #with memory.get_lock():
            """
            PcapXray runs only one O(N) packets once to memoize
            # - Parse the packets to create a usable DB
            # - All the protocol parsing should be included here
            """
                    
            for packet in self.packets: # O(N) packet iteration

                # Construct a unique key for each flow 
                source_private_ip = None
                
                ## First, Separate private vs public IPs (L3)

                # IPV6 Condition
                if "IPv6" in packet or "IPV6" in packet:

                    # Set Engine respective properties
                    if self.engine == "scapy":
                        IP = "IPv6"
                    else:
                        IP = "IPV6"

                    # TODO: Fix weird ipv6 errors in pyshark engine
                    # * ExHandler as temperory fix
                    try:
                        private_source = IPAddress(packet[IP].src).is_private()
                    except:
                        private_source = None
                    try:
                        private_destination = IPAddress(packet[IP].dst).is_private()
                    except:
                        private_destination = None
            
                elif "IP" in packet: # IPV4 Condition
                    # and packet["IP"].version == "4":
                    # Handle IP packets that originated from LAN (Internal Network)
                    #print(packet["IP"].version == "4")
                    IP = "IP"
                    private_source = IPAddress(packet[IP].src).is_private()
                    private_destination = IPAddress(packet[IP].dst).is_private()

                ## Second, Operate based on payloads above IP (L3) to create the key for session
                # <TODO> add more support as we improvise
                # Currently:
                # * TCP/UDP
                # * ICMP

                # TCP and UDP payloads
                if "TCP" in packet or "UDP" in packet:
                    
                    # Set Engine respective properties
                    if self.engine == "pyshark":
                        eth_layer = "ETH"
                        tcp_src = str(
                            packet["TCP"].srcport if "TCP" in packet else packet["UDP"].srcport)
                        tcp_dst = str(
                            packet["TCP"].dstport if "TCP" in packet else packet["UDP"].dstport)
                    else:
                        eth_layer = "Ether"
                        tcp_src = str(
                            packet["TCP"].sport if "TCP" in packet else packet["UDP"].sport)
                        tcp_dst = str(
                            packet["TCP"].dport if "TCP" in packet else packet["UDP"].dport)

                    # Session Key Creation

                    # Communication within LAN
                    if private_source and private_destination:

                        # <TODO>: Find a better way
                        # This can go either way, so first come first serve

                        key1 = packet[IP].src + "/" + packet[IP].dst + "/" + tcp_dst
                        key2 = packet[IP].dst + "/" + packet[IP].src + "/" + tcp_src

                        # First come first serve
                        if key2 in memory.packet_db:
                            source_private_ip = key2
                        else:
                            source_private_ip = key1

                        # IntraNetwork Hosts list 
                        # * When both are private they are LAN host
                        # TODO: this assumes a unique mac address per LAN, investigate if we need to account duplicate MAC
                        # * This requirement occurred when working with CTF with fake MAC like 00:00:00:00:00:00
                        if eth_layer in packet:
                            lan_key_src = packet[eth_layer].src
                            lan_key_dst = packet[eth_layer].dst
                            if lan_key_src not in memory.lan_hosts:
                                memory.lan_hosts[lan_key_src] = {"ip": packet[IP].src}
                            if lan_key_dst not in memory.lan_hosts:
                                memory.lan_hosts[lan_key_dst] = {"ip": packet[IP].dst}

                    elif private_source: # Internetwork packet

                        # Key := Always lan hosts as source in session
                        key = packet[IP].src + "/" + packet[IP].dst + "/" + tcp_dst
                        source_private_ip = key

                        # IntraNetwork vs InterNetwork Hosts list
                        if eth_layer in packet:
                            lan_key_src = packet[eth_layer].src
                            if lan_key_src not in memory.lan_hosts:
                                memory.lan_hosts[lan_key_src] = {"ip": packet[IP].src}
                            if packet[IP].dst not in memory.destination_hosts:
                                memory.destination_hosts[packet[IP].dst] = {"mac": packet[eth_layer].dst}

                    elif private_destination: # Internetwork packet

                        # Key := Always lan hosts as source in session
                        key = packet[IP].dst + "/" + packet[IP].src + "/" + tcp_src
                        source_private_ip = key

                        # IntraNetwork vs InterNetwork Hosts list
                        if eth_layer in packet:
                            lan_key_dst = packet[eth_layer].dst
                            if lan_key_dst not in memory.lan_hosts:
                                memory.lan_hosts[lan_key_dst] = {"ip": packet[IP].dst}
                            if packet[IP].src not in memory.destination_hosts:
                                memory.destination_hosts[packet[IP].src] = {"mac": packet[eth_layer].src}
                    
                    else: # public ip communication if no match

                        # <TODO>: Find a better way
                        # This can go either way, so first come first serve

                        key1 = packet[IP].src + "/" + packet[IP].dst + "/" + tcp_dst
                        key2 = packet[IP].dst + "/" + packet[IP].src + "/" + tcp_src

                        # First come first serve
                        if key2 in memory.packet_db:
                            source_private_ip = key2
                        else:
                            source_private_ip = key1

                        # IntraNetwork Hosts list 
                        # * When both are private they are LAN hosts
                        if eth_layer in packet:
                            if packet[IP].src not in memory.destination_hosts:
                                memory.destination_hosts[packet[IP].src] = {"mac": packet[eth_layer].src}
                            if packet[IP].dst not in memory.destination_hosts:
                                memory.destination_hosts[packet[IP].dst] = {"mac": packet[eth_layer].dst}
  
                elif "ICMP" in packet:

                    # Key creation similar to both private interface condition
                    key1 = packet[IP].src + "/" + packet[IP].dst + "/" + "ICMP"
                    key2 = packet[IP].dst + "/" + packet[IP].src + "/" + "ICMP"

                    # First come first serve
                    if key2 in memory.packet_db:
                        source_private_ip = key2
                    else:
                        source_private_ip = key1
                    #source_private_ip = key

                # Fill packetDB with generated key

                if source_private_ip:

                    # Unique session activity
                    if source_private_ip not in memory.packet_db:
                        memory.packet_db[source_private_ip] = {}

                        # Ethernet Layer ( Mac address )
                        if "Ethernet" not in memory.packet_db[source_private_ip]:
                            memory.packet_db[source_private_ip]["Ethernet"] = {"src":"", "dst":""}

                        # Record Payloads 
                        if "Payload" not in memory.packet_db[source_private_ip]:
                            # Record unidirectional + bidirectional separate
                            memory.packet_db[source_private_ip]["Payload"] = {"forward":[],"reverse":[]}

                        # Covert Communication Identifier
                        if "covert" not in memory.packet_db[source_private_ip]:
                            memory.packet_db[source_private_ip]["covert"] = False

                        # File Signature Identifier
                        if "file_signatures" not in memory.packet_db[source_private_ip]:
                            memory.packet_db[source_private_ip]["file_signatures"] = []
                    
                    # Covert detection and store
                    src, dst, port = source_private_ip.split("/")
                    if memory.packet_db[source_private_ip]["covert"] == False:
                        if not communication_details_fetch.trafficDetailsFetch.is_multicast(src) and not communication_details_fetch.trafficDetailsFetch.is_multicast(dst):
                            if malicious_traffic_identifier.maliciousTrafficIdentifier.covert_traffic_detection(packet) == 1:
                                memory.packet_db[source_private_ip]["covert"] = True
                        
                    # Variable to hold payload and detect covert
                    payload_string = ""

                    # Temperory Stub
                    # TODO: remove these pcap engine checks (confusing?), this is a temp block to develop/add support
                    # * Once proper building is done this would be removed
                    if self.engine == "pyshark":
                        
                        # Ethernet Layer
                        # Ethernet layer: store respect mac for the IP
                        if private_source:
                            if "ETH" in packet:
                                memory.packet_db[source_private_ip]["Ethernet"]["src"] = packet["ETH"].src
                                memory.packet_db[source_private_ip]["Ethernet"]["dst"] = packet["ETH"].dst
                            payload = "forward"
                        else:
                            if "ETH" in packet:
                                memory.packet_db[source_private_ip]["Ethernet"]["src"] = packet["ETH"].dst
                                memory.packet_db[source_private_ip]["Ethernet"]["dst"] = packet["ETH"].src
                            payload = "reverse"

                        # <TODO>: Payload recording for pyshark
                        # Refer https://github.com/KimiNewt/pyshark/issues/264
                        try:
                            memory.packet_db[source_private_ip]["Payload"][payload].append(str(packet.get_raw_packet()))
                            payload_string = packet.get_raw_packet()
                        except:
                            memory.packet_db[source_private_ip]["Payload"][payload].append("")

                    elif self.engine == "scapy":
                        
                        # Ethernet layer: store respect mac for the IP
                        if private_source:
                            if "Ether" in packet:
                                memory.packet_db[source_private_ip]["Ethernet"]["src"] = packet["Ether"].src
                                memory.packet_db[source_private_ip]["Ethernet"]["dst"] = packet["Ether"].dst
                            payload = "forward"
                        else:
                            if "Ether" in packet:
                                memory.packet_db[source_private_ip]["Ethernet"]["src"] = packet["Ether"].dst
                                memory.packet_db[source_private_ip]["Ethernet"]["dst"] = packet["Ether"].src
                            payload = "reverse"
                        
                        # Payload 
                        global tls_view_feature
                        if "TCP" in packet:
                            if tls_view_feature:
                                if "TLS" in packet:
                                    memory.packet_db[source_private_ip]["Payload"][payload].append(str(packet["TLS"].msg))
                                elif "SSLv2" in packet:
                                    memory.packet_db[source_private_ip]["Payload"][payload].append(str(packet["SSLv2"].msg))
                                elif "SSLv3" in packet:
                                    memory.packet_db[source_private_ip]["Payload"][payload].append(str(packet["SSLv3"].msg))
                                else:
                                    memory.packet_db[source_private_ip]["Payload"][payload].append(str(packet["TCP"].payload)) 
                            else:
                                # TODO: clean this payload dump
                                memory.packet_db[source_private_ip]["Payload"][payload].append(str(packet["TCP"].payload))
                            payload_string = packet["TCP"].payload
                        elif "UDP" in packet:
                            memory.packet_db[source_private_ip]["Payload"][payload].append(str(packet["UDP"].payload))
                            payload_string = packet["UDP"].payload
                        elif "ICMP" in packet:
                            memory.packet_db[source_private_ip]["Payload"][payload].append(str(packet["ICMP"].payload))
                            payload_string = packet["ICMP"].payload
                    
                    # Covert file signatures
                    if payload_string and memory.packet_db[source_private_ip]["covert"] == True:
                        file_signs = malicious_traffic_identifier.maliciousTrafficIdentifier.covert_payload_prediction(payload_string)
                        #print(file_signs)
                        if file_signs:
                            memory.packet_db[source_private_ip]["file_signatures"].extend(file_signs)
                            memory.packet_db[source_private_ip]["file_signatures"] = list(set(memory.packet_db[source_private_ip]["file_signatures"]))

    # TODO: Add function memory to store all the memory data in files (DB)
    # def memory_handle():
    """
    - Store the db as json on a file in cache folder (to not repeat read)
    """

# Local Driver
def main():
    """
    Module Driver
    """
    pcapfile = PcapEngine(sys.path[0]+'/examples/biz.pcap', "scapy")
    print(memory.packet_db.keys())
    ports = []
    
    """
    for key in memory.packet_db.keys():
    #    if "192.168.11.4" in key:
            print(key)
            print(memory.packet_db[key])
            ip, port = key.split("/")[0], int(key.split("/")[-1])
            if ip == "10.187.195.95":
                ports.append(port)
    print(sorted(list(set(ports))))
    print(memory.lan_hosts)
    print(memory.destination_hosts)
    """
    #print(memory.packet_db["TCP 192.168.0.26:64707 > 172.217.12.174:443"].summary())
    #print(memory.packet_db["TCP 172.217.12.174:443 > 192.168.0.26:64707"].summary())
    #memory.packet_db.conversations(type="jpg", target="> test.jpg")
#main()

# Sort payload by time...
# SSL Packets
# - Get handshake details?
#HTTPS
#Tor
#Malicious
# HTTP Payload Decrypt
