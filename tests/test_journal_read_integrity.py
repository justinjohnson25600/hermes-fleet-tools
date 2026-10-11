"""Read integrity must hold for every store and every public consumer."""
import contextlib
import io
import json
import pytest
from fleettools.journal import Journal, JournalCorrupt, RecordError, main, record_approval


def incident():
    return {'kind':'incident','subject':'s','reported_by':'actor','summary':'fixture'}


def approval():
    return {'subject':'s','stage':'sandbox','approved_by':'reviewer','approved_at':'2026-01-01T00:00:00Z'}


@pytest.mark.parametrize('store',['data','approvals'])
@pytest.mark.parametrize('collision',['seq','id'])
@pytest.mark.parametrize('consumer',['read','snapshot','append'])
def test_duplicate_envelope_refused_by_every_consumer(tmp_path,store,collision,consumer):
    j=Journal(tmp_path,'fixture-tenant')
    first=j.append(incident()) if store=='data' else j.append_approval(approval())
    second=dict(first,seq=2,id='fixture-unique')
    second[collision]=first[collision]
    path=j.data_path if store=='data' else j.approvals_path
    path.write_text(json.dumps(first)+'\n'+json.dumps(second)+'\n',encoding='utf-8')
    before=path.read_bytes()
    with pytest.raises(JournalCorrupt):
        if consumer=='snapshot':j.status_snapshot('s')
        elif consumer=='append':
            j.append(incident()) if store=='data' else j.append_approval(approval())
        else:j.read() if store=='data' else j.read_approvals()
    assert path.read_bytes()==before


@pytest.mark.parametrize('field',['for_incident','recurrence_of'])
@pytest.mark.parametrize('bad',[[],{},None,17,'','   '])
def test_reference_input_has_typed_refusal_without_write(tmp_path,field,bad):
    j=Journal(tmp_path,'fixture-tenant')
    first=j.append(incident())
    rec=incident() if field=='recurrence_of' else {'kind':'repair','subject':'s','performed_by':'a','outcome':'resolved','summary':'fixture'}
    rec[field]=bad
    before=j.data_path.read_bytes()
    with pytest.raises(RecordError):j.append(rec)
    assert j.data_path.read_bytes()==before


@pytest.mark.parametrize('bad',[[],{},None,17,'','   '])
def test_persisted_reference_shape_is_corruption(tmp_path,bad):
    j=Journal(tmp_path,'fixture-tenant')
    row=j.append(incident())
    row['recurrence_of']=bad
    j.data_path.write_text(json.dumps(row)+'\n',encoding='utf-8')
    with pytest.raises(JournalCorrupt):j.read()


@pytest.mark.parametrize('field,bad',[('stage','unknown'),('stage',[]),('subject',{}),('approved_by',False),('approved_at',None)])
def test_invalid_approval_history_refused_on_read_and_snapshot(tmp_path,field,bad):
    j=Journal(tmp_path,'fixture-tenant')
    row=j.append_approval(approval())
    row[field]=bad
    j.approvals_path.write_text(json.dumps(row)+'\n',encoding='utf-8')
    for operation in [j.read_approvals,lambda:j.status_snapshot('s')]:
        with pytest.raises(JournalCorrupt):operation()


@pytest.mark.parametrize('store',['data','approvals'])
def test_invalid_utf8_store_is_domain_corruption(tmp_path,store):
    j=Journal(tmp_path,'fixture-tenant')
    j.dir.mkdir()
    path=j.data_path if store=='data' else j.approvals_path
    path.write_bytes(b'\xff\n')
    with pytest.raises(JournalCorrupt):
        j.read() if store=='data' else j.read_approvals()


def test_invalid_utf8_caller_file_has_cli_exit2_no_traceback(tmp_path):
    rec=tmp_path/'record.json';rec.write_bytes(b'\xff')
    with contextlib.redirect_stderr(io.StringIO()) as output:
        rc=main(['--journal',str(tmp_path/'journals'),'--tenant','fixture','append','--file',str(rec)])
    assert rc==2
    assert 'Traceback' not in output.getvalue()


def test_unhashable_reference_has_real_cli_exit2(tmp_path):
    rec=tmp_path/'record.json'
    rec.write_text(json.dumps({'kind':'repair','subject':'s','performed_by':'a','for_incident':['x'],'outcome':'resolved','summary':'fixture'}),encoding='utf-8')
    with contextlib.redirect_stderr(io.StringIO()) as output:
        rc=main(['--journal',str(tmp_path/'journals'),'--tenant','fixture','append','--file',str(rec)])
    assert rc==2
    assert 'for_incident' in output.getvalue()


@pytest.mark.parametrize('tenant',['te\x00nant','te\x01nant','te\x7fnant','te\nnant'])
def test_tenant_control_characters_refused_at_boundary(tmp_path,tenant):
    with pytest.raises(ValueError):Journal(tmp_path,tenant)


def test_explicit_empty_approval_date_is_not_defaulted(tmp_path):
    j=Journal(tmp_path,'fixture-tenant')
    with pytest.raises(RecordError):record_approval(j,'s','sandbox','reviewer','')


@pytest.mark.parametrize('kind',[[],{}])
def test_wrong_typed_kind_is_typed_error(tmp_path,kind):
    j=Journal(tmp_path,'fixture-tenant')
    with pytest.raises(RecordError):j.append(incident()|{'kind':kind})
