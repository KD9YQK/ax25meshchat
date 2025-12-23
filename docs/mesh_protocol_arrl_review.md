# Mesh Networking Protocol — ARRL Technical & Regulatory Review

## 1. Purpose

This document provides a technical overview of a mesh networking protocol intended for use within the U.S. Amateur Radio Service. It is written specifically to support ARRL technical review, with emphasis on regulatory compliance, spectral efficiency, transparency, and adherence to established amateur digital communication practices.

The protocol is experimental, non-commercial, and designed to operate under 47 CFR Part 97.

---

## 2. Regulatory Alignment Summary

The protocol is designed to align with the following FCC regulations commonly reviewed by ARRL technical committees:

- §97.113 — Prohibited transmissions
- §97.119 — Station identification
- §97.309 — RTTY and data emission standards
- §97.305 / §97.307 — Authorized emissions and good engineering practice

Key compliance principles:
- No encryption intended to obscure message meaning
- Continuous, auditable station identification
- Publicly documented and decodable data formats
- Conservative duty cycle and bandwidth usage

---

## 3. Design Philosophy

The protocol follows long-standing amateur radio design principles:

- Transparency over obscurity
- Efficiency over throughput
- Operator responsibility over automation
- Shared-spectrum friendliness

All protocol layers are intentionally simple, inspectable, and separable.

---

## 4. Layered Architecture

Protocol responsibilities are divided as follows:

Application Layer  
- User-facing chat and messaging  
- No awareness of compression or routing  

Chat Protocol Layer  
- Structured message encoding  
- Human-readable UTF-8 payloads  

Mesh Transport Layer  
- Routing, forwarding, deduplication  
- Compression and optional encryption flags  

Link Layer  
- External modem (e.g. ARDOP)  
- RF emission characteristics  

This separation ensures that regulatory-sensitive behavior is isolated and reviewable.

---

## 5. Station Identification

Each node derives its identifier directly from the operator’s FCC-issued callsign:

- Callsign encoded into a fixed 8-byte node ID
- Node ID present in every transmitted mesh frame
- Origin of all traffic is always attributable

This design satisfies §97.119 identification requirements without relying on periodic text IDs alone.

---

## 6. Mesh Routing Behavior

### 6.1 Originator Messages (OGMs)

Routing state is maintained using small periodic beacons:

- Contain only routing metadata
- No user-generated content
- Transmitted at low, configurable intervals
- Forwarded with a strict hop limit (TTL)

This approach is comparable to other amateur mesh experiments and avoids continuous channel occupancy.

---

## 7. Application Data Transport

User messages are encapsulated in DATA frames that include:

- Origin node ID
- Destination node ID
- Sequence number
- Sender-generated message creation timestamp (application-layer metadata only)
- Explicit flags
- Application payload

The creation timestamp is used solely for display and ordering of chat history and does not affect routing, forwarding, or any RF timing behavior.

Intermediate nodes forward packets without modifying payload semantics.

---

## 8. Duplicate Suppression and Airtime Efficiency

The protocol also supports **targeted synchronization of missing messages**. When gaps are detected in received message sequences, nodes may request only the specific missing ranges rather than broad message inventories. This mechanism operates entirely at the application layer and is designed solely to reduce redundant transmissions and unnecessary airtime usage.

To reduce redundant transmissions:

- Each packet is uniquely identified by origin ID and sequence number
- Duplicate packets are dropped immediately
- Cache expiration is short and configurable

This minimizes unnecessary retransmissions and supports cooperative spectrum use.

---

## 9. Compression Policy (ARRL / FCC Perspective)

### 9.1 Purpose of Compression

Compression is used only to reduce occupied bandwidth and on-air time.

- Standard, publicly documented zlib compression
- Enabled only when payload size is reduced
- Explicitly flagged in the protocol header
- Automatically reversed by the receiver

### 9.2 Why This Is Not Encryption

Compression in this protocol does not meet the definition of encryption:

- No secret keys
- No intentional obscuring of message meaning
- Fully deterministic and reversible
- Uses widely available, documented algorithms

Any third party with protocol documentation can decode compressed payloads. This is consistent with FCC and ARRL interpretations that permit compression when meaning is not hidden.

---

## 10. Encryption Capability (Explicitly Disabled for Amateur Use)

Encryption support exists in the transport layer solely for:

- Non-amateur radio environments
- Laboratory testing
- Private or unregulated links

Default configuration:
- Encryption disabled
- Clear-text payloads only

Operators are responsible for ensuring encryption remains disabled during amateur radio operation, consistent with §97.113.

---

## 11. On-the-Wire Packet Structure

### 11.1 Mesh Frame Header (16 bytes)

All mesh frames begin with a fixed binary header:

- Version (1 byte)
- Message type (1 byte)
- Flags (1 byte)
- Time-to-live (1 byte)
- Origin node ID (8 bytes)
- Sequence number (4 bytes)

This header is always transmitted in the clear.

---

### 11.2 DATA Frame Body (Unencrypted)

- Destination node ID (8 bytes)
- Data sequence number (4 bytes)
- Application payload (N bytes)

Payload content is UTF-8 text at the application layer and remains inspectable after optional decompression.

---

## 12. Application-Layer Transparency

The chat protocol transported inside DATA frames uses:

- Simple versioned headers
- UTF-8 encoded text
- Explicit channel and sender identifiers

No proprietary encoding, scrambling, or semantic transformation is performed.

---

## 13. Duty Cycle and Spectral Considerations

The system is designed to be conservative in shared-spectrum environments:

- Small packet sizes
- No continuous transmissions
- Configurable beacon intervals (typically minutes)
- Automatic suppression of redundant traffic

Operators retain control over timing parameters to match band conditions and local practice.

---

## 14. Operator Responsibility

Final regulatory compliance depends on operator configuration and usage.

Operators must ensure:
- Appropriate band and emission selection
- Encryption disabled on amateur bands
- Reasonable duty cycle
- Proper station identification

The protocol provides compliant defaults but does not override operator responsibility.

---

## 15. Summary for ARRL Review

This mesh networking protocol is:

- Fully transparent and inspectable
- Designed for spectral efficiency
- Explicitly non-encrypted in amateur operation
- Consistent with FCC Part 97 intent
- Aligned with established amateur digital experimentation practices

The design prioritizes good engineering practice, shared-spectrum respect, and regulatory clarity.

---

End of document.


## 4.x Optional TCP Link Layer (Additional Transport)

While ARDOP remains the primary RF modem link layer, the system also supports an **optional TCP transport** for mesh frames (loopback, LAN, VPN, or WAN).

This TCP transport is implemented strictly **below** the mesh layer:
- MeshNode routing, OGMs, forwarding, TTL, and deduplication are unchanged.
- The on-the-wire mesh frame format is unchanged.
- TCP uses a simple length-prefixed framing identical in concept to the ARDOP host link framing (raw mesh frames delivered to MeshNode unchanged).
- Multiple link interfaces may be active simultaneously via a link multiplexer (ARDOP + 0..N TCP links).

This enables practical “backbone” connectivity and RF-domain bridging (e.g., VHF ↔ HF) without database sharing or application-layer hacks.
