# ARDOPC Setup Guide for ARDOP Mesh Chat

This document explains how to install, configure, and run the **ARDOP modem (ARDOPC / ARDOP_Win)** so it works reliably with this mesh chat system over **HF or VHF**.

This guide focuses on *practical, proven settings* for mesh use — not contesting or maximum-throughput point‑to‑point operation.

---

## Overview: How This Project Uses ARDOP

This mesh system connects to ARDOP via its **TCP Host Interface**.

Important implications:

- ARDOP handles **modulation, FEC, timing, and PTT**
- This software sends and receives **raw mesh frames** over TCP
- **ARDOP does NOT broadcast** — mesh broadcast behavior is implemented *above* ARDOP
- ARDOP links are treated as **half‑duplex RF pipes**

Because of this:
- Stability > raw speed
- Consistent audio levels matter more than aggressive tuning
- Lower symbol rates are often better for mesh reliability

---

## ARDOP on Windows (ARDOP_Win / ARDOPC)

### Installation
1. Download **ARDOP_Win** from the official ARDOP repository
2. Extract and run `ARDOP_Win.exe`
3. Run once as Administrator to allow audio and COM access

---

### Audio Device Configuration

In ARDOP_Win:

- **Capture device**: Radio audio output
- **Playback device**: Radio audio input
- Disable all OS audio enhancements
- Set sample rate to **48000 Hz** if available

Windows Sound Settings:
- Disable AGC, noise suppression, “communications mode”
- Set fixed levels (no auto gain)

Typical starting levels:
- TX audio: ~30–50%
- RX audio: adjust so ARDOP waterfall is clean and stable

---

### PTT Configuration (Windows)

#### Option A: Serial RTS / DTR (Most Common)

- Enable **Serial PTT**
- Select COM port (USB‑serial adapter)
- Choose **RTS** or **DTR** depending on interface
- Invert if required by your radio/interface

Common radios:
- Yaesu / Icom CAT interfaces: RTS often works
- Simple USB sound cards with GPIO: usually RTS

#### Option B: CAT PTT
- Enable CAT control if supported
- Set radio model and CAT port
- Slower but reliable

---

### TCP Host Interface Settings

Enable:
- **TCP/IP Interface**
- Default port: **8515**

This must match your `config.yaml`:

```yaml
ardop:
  host: "127.0.0.1"
  port: 8515
```

---

### Modem Speed and Bandwidth (Windows)

Recommended mesh settings:

| Band | Bandwidth | Max Speed     |
|------|-----------|---------------|
| HF   | 500 Hz    | 200–600 bps   |
| HF   | 1000 Hz   | 600–1200 bps  |
| VHF  | 2000 Hz   | 1200–2400 bps |

Notes:
- Lower speeds = fewer retries
- Avoid “Fast” modes unless link is excellent
- Consistency beats peak throughput

---

## ARDOP on Linux (ARDOPC)

### Installation

Typical build steps:
```bash
sudo apt install git build-essential libasound2-dev
git clone https://github.com/pflarue/ardop.git
cd ardop
make
```

Binary is usually `ardopc`.

---

### Running ARDOPC (Basic)

Example:
```bash
./ardopc 8515 plughw:1,0 plughw:1,0
```

Arguments:
1. TCP port (default 8515)
2. Capture device
3. Playback device

List audio devices:
```bash
arecord -l
aplay -l
```

---

### Example: HF Configuration (Linux)

```bash
./ardopc 8515 plughw:1,0 plughw:1,0   --samplerate 48000   --bw 1000   --txdelay 300   --pttserial /dev/ttyUSB0   --pttmode RTS
```

Good for:
- NVIS
- 40m / 80m
- Shared mesh channels

---

### Example: VHF Configuration (Linux)

```bash
./ardopc 8515 plughw:2,0 plughw:2,0   --samplerate 48000   --bw 2000   --txdelay 150   --pttserial /dev/ttyUSB0   --pttmode RTS
```

Good for:
- 2m / 70cm
- Line‑of‑sight mesh
- Backbone links

---

### PTT on Linux

#### Serial RTS/DTR
- Most reliable
- USB‑serial adapter recommended
- Common interfaces: SignaLink, homebrew VOX‑less cables

Example:
```bash
--pttserial /dev/ttyUSB0 --pttmode RTS
```

#### GPIO (Raspberry Pi)
- Supported in ARDOPC builds with GPIO enabled
- Requires correct pin mapping and permissions

---

## Audio Level Tuning (Critical)

### RX
- Waterfall should be clear
- No clipping
- No “fuzzy” edges

### TX
- Avoid ALC activation
- Flat audio
- Do NOT overdrive

Rule of thumb:
> If it sounds boring, it’s probably perfect.

---

## Common Problems

### ARDOP connects but no traffic flows
- TCP port mismatch
- Wrong audio device index
- PTT not asserting

### Frequent retries / disconnects
- TX audio too hot
- Bandwidth too high
- Wrong symbol rate for channel

### Works point‑to‑point but mesh feels slow
- That’s normal
- Mesh favors reliability and discovery over peak throughput

---

## Recommended Defaults for This Project

- Bandwidth: **1000 Hz**
- Conservative symbol rate
- Stable PTT (RTS/DTR)
- Fixed audio levels
- Leave ARDOP auto‑modes enabled

This mesh software handles:
- Discovery
- Routing
- Deduplication
- Reliability

ARDOP’s job is simply to move bits cleanly.

---

## Final Notes

- Use **fake_ardopc.py** for development and testing
- Test RF links with short sessions first
- Keep logs enabled while tuning
- When in doubt, slow down

Stable links beat fast links — especially in a mesh.
