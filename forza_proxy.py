"""PROXY protocol version 2 from a trusted tunnel agent (playit.gg): who a tunnelled player is.

A tunnel agent relays every player's UDP and TCP to the server from its own sockets, so the
server sees only the agent (forza_limits.TRUSTED_PROXIES) and cannot tell other players where
to punch to. With the tunnel's Proxy Protocol set to proxy-protocol-v2, the agent says whose
each flow is in the binary header of HAProxy's PROXY protocol (proxy-protocol.txt, 2.2):

- UDP: playit agents from 0.16 send the header as a datagram of its own, from the flow's
  socket, before the flow's first datagram: once per flow. The agent forgets a flow that
  carried nothing for 60 to 90 s; the next datagram opens a new one, with a new header. A
  flow that began before the setting was turned on, or before the server restarted, gets no
  header. Agents up to 0.15.26 put the header in front of every datagram instead. Both
  work: parse() returns what follows the header. Version 1 (text) is never sent on UDP.
- TCP: the header is the first thing on the connection, once.

Agents 0.16.0 to 0.16.4 swapped the two addresses of the UDP header (the tunnel's address
where the player's belongs; fixed in 0.16.5): use a current agent.

Only a trusted proxy's header is believed. From anywhere else, bytes that start with the
signature are neither a game datagram nor an HTTP request and are dropped, as before.
Standard library only.
"""

import ipaddress
import struct

import forza_limits as limits

SIGNATURE = b"\r\n\r\n\x00\r\nQUIT\n"
HEADER_SIZE = 16  # the signature, version and command, family and transport, length
MAX_LENGTH = 512  # bytes of addresses and TLVs after the 16 (playit sends 12 or 36)
LOCAL, PROXY = 0, 1  # commands (the low nibble of byte 12; its high nibble is the version, 2)
UNSPEC, INET, INET6, UNIX = 0, 1, 2, 3  # address families (the high nibble of byte 13)
STREAM, DGRAM = 1, 2  # transports (its low nibble): TCP, UDP
ADDRESS_SIZE = {INET: 12, INET6: 36, UNIX: 216}


def header_length(data):
    """The size of the whole header from its first 16 bytes. ValueError when they are not
    the start of a valid version 2 header."""
    if len(data) < HEADER_SIZE or not data.startswith(SIGNATURE):
        raise ValueError("no PROXY v2 signature")
    if data[12] >> 4 != 2:
        raise ValueError("PROXY version %d" % (data[12] >> 4))
    if data[12] & 0xF not in (LOCAL, PROXY):
        raise ValueError("PROXY command %d" % (data[12] & 0xF))
    (length,) = struct.unpack_from(">H", data, 14)
    if length > MAX_LENGTH:
        raise ValueError("PROXY header of %d bytes" % length)
    return HEADER_SIZE + length


def parse(data, transport):
    """A version 2 header at the start of data, sent for a `transport` (STREAM or DGRAM)
    flow. Returns (source, rest): the client's (ip, port), None when the header names no
    IP address (the LOCAL command, an unspecified or a UNIX family), and the bytes after the
    header. ValueError when anything is malformed: the signature, version, command, family,
    transport, lengths or the TLVs after the addresses."""
    size = header_length(data)
    if len(data) < size:
        raise ValueError("PROXY header truncated")
    rest = data[size:]
    if data[12] & 0xF == LOCAL:  # the proxy's own connection: no addresses to use
        return None, rest
    family, kind = data[13] >> 4, data[13] & 0xF
    if (family, kind) == (UNSPEC, 0):
        return None, rest
    if family not in ADDRESS_SIZE or kind != transport:
        raise ValueError("PROXY family/transport %02X" % data[13])
    need = ADDRESS_SIZE[family]
    if size - HEADER_SIZE < need:
        raise ValueError("PROXY addresses of %d bytes" % (size - HEADER_SIZE))
    pos = HEADER_SIZE + need
    while pos < size:  # TLVs: type u8, length u16, value (none from playit; skipped)
        if pos + 3 > size:
            raise ValueError("PROXY TLV truncated")
        (tlv,) = struct.unpack_from(">H", data, pos + 1)
        pos += 3 + tlv
        if pos > size:
            raise ValueError("PROXY TLV truncated")
    if family == UNIX:
        return None, rest
    if family == INET:
        ip = ipaddress.IPv4Address(bytes(data[16:20]))
        (port,) = struct.unpack_from(">H", data, 24)
    else:
        ip = ipaddress.IPv6Address(bytes(data[16:32]))
        ip = ip.ipv4_mapped or ip
        (port,) = struct.unpack_from(">H", data, 48)
    return (str(ip), port), rest


def client_address(source, ipv4=False):
    """source when it can be a player's address on the internet; None for no address, a
    port 0, an unspecified, loopback, link-local, multicast or reserved address, or a
    trusted proxy's. ipv4: IPv4 only (the game's protocol carries no IPv6 address)."""
    if not source or not source[1]:
        return None
    try:
        ip = ipaddress.ip_address(source[0])
    except ValueError:
        return None
    if ipv4 and ip.version != 4:
        return None
    if (ip.is_unspecified or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or limits.is_trusted_proxy(str(ip))):
        return None
    return (str(ip), source[1])


def header(source, destination, transport=DGRAM, command=PROXY, tlvs=b""):
    """A version 2 header as a playit agent writes it (for tests and tools)."""
    src, dst = ipaddress.ip_address(source[0]), ipaddress.ip_address(destination[0])
    family = INET if src.version == 4 else INET6
    body = src.packed + dst.packed + struct.pack(">HH", source[1], destination[1]) + tlvs
    return (SIGNATURE + bytes([0x20 | command, family << 4 | transport]) +
            struct.pack(">H", len(body)) + body)
