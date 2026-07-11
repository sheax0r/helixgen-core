#!/usr/bin/env python3
"""Full-CRUD proof over the network — no Helix editor involved.

Creates a copy of a preset into the empty USER 2D slot (pos 7), renames it,
then deletes it, verifying each step by re-listing. Fully reversible: the slot
starts and ends empty.
"""
import sys, time
from helix_net import HelixClient, USER

POS = 7           # USER slot 2D (empty by default)
SRC_CID = 904     # "Dream On" (USER 1A) — the preset we copy


def show(h, tag):
    ps = h.list_presets(USER)
    at = next((m for m in ps if m.get('posi') == POS), None)
    print('  [%s] slot %d = %s   (USER has %d presets)' % (
        tag, POS, (at.get('name') if at else 'Empty'), len(ps)))
    return at


def main():
    ip = sys.argv[1] if len(sys.argv) > 1 else '192.168.4.84'
    h = HelixClient(ip).connect()
    print('connected to %s\n' % ip)

    before = show(h, 'before')
    if before:
        print('  ! slot %d is not empty — aborting so we do not clobber it' % POS)
        return

    print('\nCREATE: copy cid %d -> USER slot %d' % (SRC_CID, POS))
    print('  ok=%s' % h.copy_into(USER, [SRC_CID], POS))
    time.sleep(0.5)
    created = show(h, 'after create')
    if not created:
        print('  ! create did not appear'); return
    new_cid = created['cid_']
    print('  new preset cid=%d name=%r' % (new_cid, created.get('name')))

    print('\nUPDATE: rename cid %d -> "Net CRUD Proof"' % new_cid)
    print('  ok=%s' % h.set_name(new_cid, 'Net CRUD Proof'))
    time.sleep(0.5)
    show(h, 'after rename')

    print('\nREAD: get_ref(cid %d)' % new_cid)
    print('  %s' % h.get_ref(new_cid))

    print('\nDELETE: remove cid %d from USER' % new_cid)
    print('  ok=%s' % h.remove(USER, [new_cid]))
    time.sleep(0.5)
    after = show(h, 'after delete')
    print('\nRESULT: %s' % ('PASS — slot empty again, full CRUD cycle worked'
                            if after is None else 'FAIL — slot still occupied'))
    h.close()


if __name__ == '__main__':
    main()
