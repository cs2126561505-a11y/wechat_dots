"""Private attempt recovery uses synthetic data only; no UI or live reads."""
import json
from pathlib import Path
import tempfile
import unittest
from wechat_probe.core import digest
from wechat_probe.sender import unresolved_attempts


class SenderRecoveryTests(unittest.TestCase):
    def test_unknown_attempt_stays_blocking_across_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            run=Path(tmp).resolve()
            old=run/'send-original';old.mkdir()
            (old/'native-attempt.json').write_text('{}')
            self.assertEqual(unresolved_attempts(run),['original'])
            self.assertEqual(unresolved_attempts(Path(tmp).resolve()),['original'])

    def test_owner_followup_requires_matching_receipt_and_preserves_original_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            run=Path(tmp).resolve()
            old=run/'send-original';old.mkdir()
            following=run/'send-following';following.mkdir()
            def put(path,data):path.write_text(json.dumps(data),encoding='utf-8')
            put(old/'native-attempt.json',{})
            put(old/'request.json',{'command':{'text':'approved','account_id':'wxid_self','room_id':'room@chatroom'}})
            put(old/'owner-resolution.json',{'owner_authorized_followup':True,'followup_operation':'following'})
            put(following/'request.json',{'supersedes':'original'})
            receipt={'body_sha256':digest('wrong'),'account_id':'wxid_self','room_id':'room@chatroom'}
            put(following/'receipt.json',receipt)
            self.assertEqual(unresolved_attempts(run),['original'])
            receipt['body_sha256']=digest('approved');put(following/'receipt.json',receipt)
            self.assertEqual(unresolved_attempts(run),[])
            self.assertTrue((old/'native-attempt.json').is_file())
            self.assertFalse((old/'receipt.json').exists())


if __name__=='__main__':unittest.main()
