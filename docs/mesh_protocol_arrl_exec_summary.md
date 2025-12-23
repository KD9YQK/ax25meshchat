# Mesh Networking Protocol — Executive Summary for ARRL Review

## Overview

This project implements an experimental amateur radio mesh networking protocol designed to operate within FCC Part 97 regulations. The system prioritizes transparency, spectral efficiency, and operator accountability.

The protocol supports low-bandwidth digital messaging over shared RF spectrum using conservative duty cycles, publicly documented formats, and optional **targeted message recovery** to minimize redundant retransmissions.

Chat messages include a sender-generated UTC creation timestamp used solely for human-readable display and deterministic history ordering; this metadata does not influence routing, transmission timing, or RF behavior.

## Optional TCP-Based Mesh Links (Non-RF Transport)

In addition to ARDOP-over-RF, the system can optionally carry the *same* mesh frames over TCP sockets (loopback, LAN, VPN, or WAN) as an alternative link transport.

Key points:
- **No protocol changes:** the mesh header, routing, TTL, dedup, and chat payload formats are identical.
- **Same framing model as ARDOP link:** raw mesh frames are sent with a simple length-prefix framing over TCP.
- **Multiple simultaneous links:** a node may run ARDOP and one or more TCP links at the same time (multiplexed below the mesh layer).
- **Bridging/backbone support:** TCP links can provide a wired backbone or connect dissimilar RF domains (e.g., VHF ↔ HF) without DB sharing.
- **Link-local authentication (TCP only):** TCP links may require a password to prevent unintended peering; this is not used to obscure message meaning.

This feature is disabled by default and does not affect RF behavior when unused.

## Key Compliance Points

- No encryption is used during amateur radio operation
- Compression is used only to reduce airtime and does not obscure meaning
- All packets include callsign-derived node identification
- Protocol behavior is fully documented and auditable
- Operators retain full control over timing and configuration

## Intended Use

- Amateur experimentation
- Emergency and field communications research
- Educational and club-based mesh networking projects

This protocol is not intended for commercial use or unattended wide-area automation.

## Summary

The design aligns with FCC Part 97 intent and established amateur digital communication practices. It is suitable for experimental deployment and technical review within the amateur radio community.
