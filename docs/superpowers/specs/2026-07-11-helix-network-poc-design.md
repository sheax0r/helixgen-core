# Helix Stadium network POC — plan of attack

**Date:** 2026-07-11
**Status:** design — awaiting review
**Branch/worktree:** `worktree-helix-net-poc` (throwaway; push only if it pans out)

## Goal

A standalone script that connects to the Helix Stadium over the LAN and prints
the **list of presets on the device**. This is a pure spike to prove network
control is feasible. "Whatever works first" — get a preset list out by any
means; choose the durable architecture afterward.

## Non-goals (this round)

- No writes/edits to the device (read-only against the hardware).
- No integration with helixgen (no imports, its own directory).
- No polished CLI, no config, no packaging. A single script is fine.
- Not committing to a transport as "the" answer yet.

## What recon already established

- The **debug build** of the editor (`~/Helix Stadium Debug.app`, debugger
  restriction removed) is running as PID **88231** and is **connected now**.
- The **device is at `192.168.4.84`** on the LAN (Mac is `192.168.4.97`).
- The app holds four live TCP connections to the device:
  - `:2001`, `:2002`, `:2003` — three custom control/data/event ports (plain TCP).
  - `:22` — **SSH**. The device runs a real OS with an sshd.
- Tooling present: `lldb`, `dtrace`/`dtruss`, `fs_usage`, `ktrace`, `tcpdump`.
  Missing (installable via Homebrew if needed): Wireshark/`tshark`, Frida,
  mitmproxy.
- The app is debugger-attachable, so live hooking is on the table.

## Approaches (three avenues, sequenced not exclusive)

**A. Passive wire capture** — `tcpdump` on `en0` filtered to `host 192.168.4.84`,
trigger a preset-list refresh, correlate the burst to identify the port + framing
carrying preset names. Least invasive; may crack it outright if cleartext.
Risk: TLS/opaque framing stalls it.

**B. Live app instrumentation** — attach Frida/`lldb` to PID 88231, hook
`send`/`recv` (and any TLS read/write) to dump decrypted payloads with call
stacks. Beats capture when the wire is opaque; reveals framing + exact request
bytes to replay. Fallback if A is opaque.

**C. Direct device access via SSH** — presets may be files on the device
filesystem; "list presets" could be an SSH command, skipping protocol reversing
entirely. Highest leverage if auth is obtainable; may not reflect unsaved/edited
live state.

## Chosen plan

Run **A + C as cheap parallel recon first**, escalate to **B** only for whatever
detail stays opaque. Deliverable is a small **stdlib-only Python** script.

### Phase 0 — Surface map (read-only, ~10 min)
Identify each port. SSH banner grab on `:22`. Determine whether `:2001–2003` are
cleartext or TLS from captured first bytes (not by connecting yet).
**Deliverable:** one-paragraph "what each port is" note.

### Phase 1 — Passive capture of a real "list presets" (~20 min)
`sudo tcpdump -i en0 -w helix.pcap host 192.168.4.84` (needs sudo approval).
**Claude drives the app UI** (macOS accessibility / AppleScript / `cliclick`)
to trigger a preset-list refresh, correlating the click to the packet burst.
Identify the port + message framing carrying preset names.
**Deliverable:** annotated capture; the request/response for "list presets".

### Phase 2 — Live instrumentation (only if the wire is opaque)
Install Frida; attach to PID 88231; hook `send`/`recv` (+ TLS read/write) to dump
decrypted payloads with call stacks. `lldb` is the fallback on the hardened
runtime.
**Deliverable:** the exact plaintext request bytes + response framing.

### Phase 3 — Minimal client + replay
Write `helix_probe.py`: open the connection, perform the handshake revealed by
Phase 1/2, send the "list presets" request, parse the response, print names. In
parallel, cheaply test the SSH route from Phase 0.
**Deliverable:** a script that prints the device's preset list.

### Phase 4 — Validate
Cross-check the printed list against the app UI. **Done when they match.**

## Guardrails

- Read-only against the device this round (list/read only).
- Everything lives in this worktree; no helixgen imports.
- Stdlib-only Python for the probe (matches helixgen conventions).
- `sudo`/tool installs surfaced to the user for approval as they arise.

## Open questions / risks

- `:2001–2003` may be TLS → forces Phase 2 (instrumentation).
- SSH may require a key/password we don't have → route C may be a dead end.
- Preset "list" may be device-state served over the protocol rather than files,
  making route A/B the only faithful source of live/edited presets.
