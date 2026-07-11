#!/usr/bin/env python3
"""Spawn Helix Stadium under Frida so we capture the connect-time preset sync.

Captures every byte from launch (including the initial preset-list enumeration
the app does once at connect) to captures/raw_spawn.jsonl.
"""
import sys, os, time, json, base64
import frida

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'hook_sockets.js')
CAPDIR = os.path.join(HERE, '..', 'captures')
os.makedirs(CAPDIR, exist_ok=True)
RAW = os.path.join(CAPDIR, 'raw_spawn.jsonl')
BIN = '/Users/michael.shea/Helix Stadium Debug.app/Contents/MacOS/Helix Stadium'

def main():
    rawf = open(RAW, 'w', buffering=1)
    dev = frida.get_local_device()
    print('%.3f SPAWN %s' % (time.time(), BIN), flush=True)
    pid = dev.spawn([BIN])
    session = dev.attach(pid)
    with open(SCRIPT) as f:
        script = session.create_script(f.read())

    def on_message(msg, data):
        if msg['type'] == 'error':
            print('ERROR', json.dumps(msg), flush=True); return
        if msg['type'] != 'send':
            return
        p = msg['payload']
        if p.get('dir') == 'INFO':
            print('%.3f INFO %s' % (time.time(), p.get('msg')), flush=True); return
        ts = time.time()
        rec = dict(p); rec['ts'] = ts
        rec['b64'] = base64.b64encode(data).decode() if data else ''
        rawf.write(json.dumps(rec) + '\n')
        if p.get('addr') not in ('/trigger', '/dspEvent', '/heartbeat'):
            print('%.3f %-5s %-6d %-20s len=%d' % (
                ts, p['dir'].split('[')[0], p.get('port', 0), p.get('addr', '?'), p.get('len', 0)), flush=True)

    script.on('message', on_message)
    script.load()
    dev.resume(pid)
    print('%.3f resumed pid=%d, capturing connect sync' % (time.time(), pid), flush=True)
    while True:
        time.sleep(3600)

if __name__ == '__main__':
    main()
