"""Commit sequence and same-instance transaction ownership are load-bearing."""
from concurrent.futures import ThreadPoolExecutor
import pytest
from fleettools.journal import Journal, JournalLocked, assess


def incident(j, **kwargs):
    return j.append({'kind':'incident','subject':'s','reported_by':'actor-a',
                     'summary':'fixture',**kwargs}, ts='2020-01-01T00:00:00Z')


def repair(j, inc, ts='2026-01-03T00:00:00Z'):
    return j.append({'kind':'repair','subject':'s','performed_by':'actor-b',
                     'for_incident':inc['id'],'outcome':'resolved','summary':'fixture',
                     'evidence':[{'source':'monitor','ref':'m1','verified':True},
                                 {'source':'audit','ref':'a1','verified':True}]}, ts=ts)


def test_later_committed_backdated_recurrence_blocks(tmp_path):
    j=Journal(tmp_path,'fixture-tenant')
    first=incident(j)
    repair(j,first)
    later=incident(j,recurrence_of=first['id'])
    assert later['seq'] > j.read()[1]['seq']
    assert assess(j.read(),'s')['verdict']=='INELIGIBLE'


def test_later_repair_settles_chain_even_with_earlier_display_date(tmp_path):
    j=Journal(tmp_path,'fixture-tenant')
    first=incident(j)
    repair(j,first)
    recurrence=incident(j,recurrence_of=first['id'])
    repair(j,recurrence,ts='2019-01-01T00:00:00Z')
    assert assess(j.read(),'s')['verdict']=='ELIGIBLE'


def test_other_thread_cannot_reuse_owner_transaction(tmp_path):
    j=Journal(tmp_path,'fixture-tenant',lock_timeout=0.05)
    incident(j)
    j._acquire()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(JournalLocked):
                pool.submit(j.read).result(timeout=2)
    finally:
        j._release()
    assert len(j.read())==1


def test_owner_can_read_its_own_transaction(tmp_path):
    j=Journal(tmp_path,'fixture-tenant',lock_timeout=0.05)
    incident(j)
    j._acquire()
    try:
        assert len(j.read())==1
    finally:
        j._release()
