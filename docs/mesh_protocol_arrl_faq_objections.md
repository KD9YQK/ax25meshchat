# Frequently Raised Questions and Responses (ARRL Review)

## Is this encryption?

No. Compression is used only to reduce airtime. No secret keys are used, and all payloads are reversible using public algorithms.

## Can messages be intercepted?

Yes. Any third party with protocol documentation can decode all transmissions.

## Is store-and-forward allowed?

Yes. Store-and-forward messaging has long precedent in amateur radio (e.g., packet BBS systems).

## Does this hide station identification?

No. Callsign-derived node IDs are included in every transmitted frame.

## Is this unattended operation?

No. Operators configure and control all nodes and timing parameters.

## Does this comply with Part 97 intent?

Yes.

## Does targeted synchronization change protocol behavior?

No. Targeted synchronization is an application-layer efficiency feature that requests only specific missing message ranges. It does not alter framing, routing, station identification, duty cycle requirements, or emission characteristics. The protocol emphasizes transparency, shared-spectrum respect, and experimental use consistent with amateur radio principles.

## What about TCP links, passwords, or “internet bridging”?

The protocol can optionally run the same mesh frames over TCP (loopback/LAN/VPN/WAN) in addition to ARDOP. This does **not** change the over-the-air protocol or packet formats. It simply adds another link-layer transport.

- **Passwords are link-local access control for TCP peering**, not encryption of message content. They prevent accidental or unauthorized TCP neighbors from joining a node.
- The **payload remains fully decodable** by anyone with the protocol documentation (and with RF reception, for amateur operation).
- TCP links are intended for experimentation and for connecting RF “islands” via a wired backbone (e.g., an emergency-operations LAN), while keeping the mesh routing logic identical.
