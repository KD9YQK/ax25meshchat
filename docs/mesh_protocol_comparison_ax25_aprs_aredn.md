# Mesh Protocol Comparison — AX.25, APRS, and AREDN

## AX.25

- Frame-based link protocol
- Explicit callsign identification
- Widely accepted under Part 97
- Inefficient for multi-hop dynamic mesh routing

## APRS

- Application built on AX.25
- Position and message-centric
- High redundancy and repeated transmissions
- Limited support for dynamic routing

## AREDN

- IP-based microwave mesh
- Uses Part 97 allocations
- High throughput
- Requires wide bandwidth and specialized hardware

## This Mesh Protocol

- Lightweight, non-IP mesh routing
- Optimized for narrowband links
- Explicit callsign-based node IDs
- Lower duty cycle and bandwidth usage
- Designed for HF/VHF experimentation

## Summary

This protocol occupies a middle ground between AX.25 simplicity and AREDN throughput, prioritizing efficient narrowband experimentation.

## TCP-Based Backbones (Optional)

This system can optionally carry the same mesh frames over TCP sockets (LAN/VPN/WAN) as a secondary link transport. This is not an IP-routed mesh like AREDN; it is simply an additional link layer beneath the existing mesh protocol, used for:
- Wired backbones between RF islands
- Bridging different RF domains (e.g., VHF ↔ HF)
- Loopback/LAN testing without RF

Routing behavior, TTL, deduplication, and message formats remain identical.
