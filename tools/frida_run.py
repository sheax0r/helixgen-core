#!/usr/bin/env python3
"""Attach the OSC socket-hook to the running Helix Stadium app.

Console: one readable line per non-noise message.
captures/raw.jsonl: full base64 payloads for offline decoding.
"""
import sys, os, time, json, base64
import frida

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'hook_sockets.js')
CAPDIR = os.path.join(HERE, '..', 'captures')
os.makedirs(CAPDIR, exist_ok=True)
RAW = os.path.join(CAPDIR, 'raw.jsonl')

def find_pid():
    for p in frida.get_local_device().enumerate_processes():
        if p.name.startswith('Helix'):
            return p.pid, p.name
    raise SystemExit('Helix process not found')

def main():
    pid, name = find_pid()
    rawf = open(RAW, 'a', buffering=1)
    print('%.3f ATTACH pid=%d %r' % (time.time(), pid, name), flush=True)
    session = frida.attach(pid)
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
        print('%.3f %-4s %-6d %-16s len=%d' % (
            ts, p['dir'], p.get('port', 0), p.get('addr', '?'), p.get('len', 0)), flush=True)

    script.on('message', on_message)
    script.load()
    print('%.3f loaded, capturing' % time.time(), flush=True)
    while True:
        time.sleep(3600)

if __name__ == '__main__':
    main()
