"""JSON syntax is not a guarantee of UTF-8 encodable string data."""
import contextlib
import io
import json
import pytest
from fleettools.journal import Journal, JournalCorrupt, RecordError, main


def incident() -> dict:
    return {'kind':'incident','subject':'s','reported_by':'actor','summary':'fixture'}


@pytest.mark.parametrize('field',['subject','summary','reported_by','evidence-source','evidence-ref','unknown-key'])
@pytest.mark.parametrize('bad',['\ud800','\udfff'])
def test_surrogate_caller_strings_refused_before_write(tmp_path,field,bad):
    j=Journal(tmp_path,'fixture-tenant')
    j.append(incident())
    before=j.data_path.read_bytes()
    rec=incident()
    if field.startswith('evidence-'):
        evidence={'source':'monitor','ref':'fixture','verified':True}
        evidence[field.split('-')[1]]=bad
        rec['evidence']=[evidence]
    elif field=='unknown-key':rec[bad]='fixture'
    else:rec[field]=bad
    with pytest.raises(RecordError):j.append(rec)
    assert j.data_path.read_bytes()==before
    assert not list(j.dir.glob('*.tmp'))


@pytest.mark.parametrize('field',['subject','approved_by'])
def test_approval_surrogate_has_real_cli_exit2(tmp_path,field):
    argv=['--journal',str(tmp_path),'--tenant','fixture','approve','--stage','sandbox',
          '--subject','s','--approved-by','reviewer']
    argv[argv.index('--'+field.replace('_','-'))+1]='\ud800'
    with contextlib.redirect_stderr(io.StringIO()) as output:
        rc=main(argv)
    assert rc==2
    assert 'Traceback' not in output.getvalue()
    assert not (tmp_path/'fixture'/'approvals.jsonl').exists()


def test_escaped_surrogate_in_cli_record_file_is_exit2(tmp_path):
    path=tmp_path/'record.json'
    path.write_text(json.dumps(incident()|{'subject':'\ud800'}),encoding='utf-8')
    with contextlib.redirect_stderr(io.StringIO()) as output:
        rc=main(['--journal',str(tmp_path/'journals'),'--tenant','fixture','append','--file',str(path)])
    assert rc==2
    assert 'Traceback' not in output.getvalue()


@pytest.mark.parametrize('store',['data','approvals'])
def test_escaped_surrogate_in_persisted_store_is_corruption(tmp_path,store):
    j=Journal(tmp_path,'fixture-tenant')
    row=j.append(incident()) if store=='data' else j.append_approval(
        {'subject':'s','stage':'sandbox','approved_by':'reviewer','approved_at':'2026-01-01T00:00:00Z'})
    row['subject']='\ud800'
    path=j.data_path if store=='data' else j.approvals_path
    path.write_text(json.dumps(row)+'\n',encoding='utf-8')
    with pytest.raises(JournalCorrupt):j.read() if store=='data' else j.read_approvals()


@pytest.mark.parametrize('store',['data','approvals'])
def test_bare_cr_is_not_a_committed_line_delimiter(tmp_path,store):
    j=Journal(tmp_path,'fixture-tenant')
    row=j.append(incident()) if store=='data' else j.append_approval(
        {'subject':'s','stage':'sandbox','approved_by':'reviewer','approved_at':'2026-01-01T00:00:00Z'})
    path=j.data_path if store=='data' else j.approvals_path
    path.write_bytes(json.dumps(row).encode('utf-8')+b'\r')
    with pytest.raises(JournalCorrupt):j.read() if store=='data' else j.read_approvals()


def test_crlf_is_still_a_complete_legacy_jsonl_line(tmp_path):
    j=Journal(tmp_path,'fixture-tenant')
    row=j.append(incident())
    j.data_path.write_bytes(json.dumps(row).encode('utf-8')+b'\r\n')
    assert j.read()==[row]


def test_valid_unicode_roundtrip_is_preserved(tmp_path):
    j=Journal(tmp_path,'fixture-tenant')
    row=j.append(incident()|{'summary':'fixture 日本語 😀 e\u0301'})
    assert j.read()==[row]
