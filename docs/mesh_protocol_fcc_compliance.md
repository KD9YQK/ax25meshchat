# Mesh Networking Protocol — FCC Compliance Overview

## 1. Purpose and Scope

This document describes the design and operation of the mesh networking protocol used by this system, with specific attention to compliance with United States Federal Communications Commission (FCC) regulations governing the Amateur Radio Service (47 CFR Part 97).

The protocol is intended for experimental, non-commercial amateur radio use and emphasizes:
- Operator accountability
- Open, inspectable data formats
- Efficient spectrum utilization
- Explicit avoidance of prohibited encryption or obscuring techniques

This document addresses protocol behavior and technical design, not operator licensing or band authorization.

## 2. Regulatory Context (FCC Part 97)

Relevant FCC regulations addressed by this design include:
- §97.113 — Prohibited transmissions
- §97.119 — Station identification
- §97.309 — Emission standards
- §97.305 / §97.307 — Authorized bandwidth and good engineering practice

Under Part 97:
- Messages must not be encrypted to obscure their meaning
- Station identification must be possible at all times
- Digital protocols must be publicly documented or readily decodable
- Efficient and cooperative spectrum use is expected

## 3. System Architecture Overview

Layered architecture:

- Application (Chat / UI)  
- Chat Protocol  
- Mesh Transport Protocol  
- Link Layer (e.g. ARDOP modem)  
- RF Medium

All FCC-sensitive behavior (routing, forwarding, compression, optional encryption) is confined to the mesh transport layer.

## 4. Node Identification and Callsign Usage

Each mesh node derives its unique identifier directly from the operator’s FCC-issued amateur radio callsign.

- Callsigns are encoded into a fixed 8-byte node identifier
- The identifier is included in every mesh frame header
- Originating station identity is never hidden or randomized

This satisfies §97.119 identification requirements.

## 5. Mesh Routing and Control Traffic

### 5.1 Originator Messages (OGMs)

Nodes periodically transmit routing beacons used to maintain network topology.

Characteristics:
- Fixed, compact binary format
- Routing metadata only
- No user content
- Low duty cycle
- TTL-limited forwarding

## 6. Application Data Transport

User-generated content is transported inside mesh DATA frames containing:
- Origin node ID
- Destination node ID
- Sequence number
- Flags
- Application payload

Application-layer messages may also include sender-generated timestamps for display and ordering; these values are not used to control transmission timing, duty cycle, or channel access.

## 7. Deduplication and Forwarding

Each DATA frame is uniquely identified by (origin_id, sequence_number).  
Duplicate packets are dropped to reduce channel congestion.

## 8. Compression (Not Encryption)

### 8.1 Purpose

Compression is used solely to reduce on-air time and bandwidth usage.

- Standard zlib compression
- Enabled only if payload size is reduced
- Explicitly flagged
- Automatically reversed by the receiver

### 8.2 Why Compression Is Not Encryption

Compression does not constitute encryption:
- No secret keys
- No semantic hiding
- Fully deterministic and reversible
- Uses public algorithms

Any third party may decode compressed frames using documented methods.

## 9. Encryption Policy

### 9.1 Default Operation

Encryption is disabled by default.  
All payloads are transmitted in clear, decodable form.

### 9.2 Optional Encryption

Encryption support exists only for:
- Non-amateur bands
- Laboratory testing
- Private or unregulated links

Operators must ensure encryption is disabled during amateur operation.

## 10. Packet Format Details

### 10.1 Mesh Header (16 bytes)

Fields:
- Version (1 byte)
- Message Type (1 byte)
- Flags (1 byte)
- TTL (1 byte)
- Origin Node ID (8 bytes)
- Sequence Number (4 bytes)

The header is always transmitted in the clear.

### 10.2 DATA Frame Body (Unencrypted)

- Destination Node ID (8 bytes)
- Data Sequence Number (4 bytes)
- Application Payload (N bytes)

Payload may be compressed but not obscured.

### 10.3 DATA Frame Body (Encrypted — Non-Amateur Use Only)

- Destination Node ID (8 bytes)
- Data Sequence Number (4 bytes)
- Nonce (12 bytes)
- Encrypted Payload (N bytes)

Encryption is explicitly flagged.

## 11. Application-Level Transparency

The chat protocol uses:
- Versioned headers
- UTF-8 text
- Explicit channel and sender identifiers

No proprietary encoding or scrambling is used.

## 12. Bandwidth and Duty Cycle

Design considerations include:
- Small packet sizes
- Configurable beacon intervals
- No continuous transmissions
- Redundant traffic suppression

## 13. Operator Responsibility

Operators must ensure:
- Legal band usage
- Approved emission modes
- Encryption disabled on amateur bands
- Reasonable duty cycle
- Proper station identification

## 14. Summary

This mesh protocol is open, inspectable, spectrally efficient, and aligned with FCC Part 97 requirements.

Compression reduces airtime only and does not obscure meaning.  
Packet formats are fully documented and auditable.
