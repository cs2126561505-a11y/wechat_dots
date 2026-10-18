"""Synthetic admission evidence only; no real host or guest identity."""
import copy
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from wechat_probe.core import ProbeError
from wechat_probe.isolation import validate_lease, check_lease_still_active, validate_smb_report, check_account_path, CLIENT_SHA256


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.facts = dict(model="VirtualBox", elevated=False, uuid="test-uuid", machine_guid="test-guid",
                          sid="test-sid", profile="test-profile", network_drives=0, smb_connections=0,
                          sync_processes=0, client_sha256=CLIENT_SHA256)
        self.process = dict(pid=12, started=123.0, exe="test-client")
        self.lease = dict(schema=1, scope="test-group-read-only", issued_at=100, expires_at=200,
                          process=self.process, account_dir="test-account",
                          **{k:self.facts[k] for k in ("uuid", "machine_guid", "sid", "profile")},
                          host_checks={k:True for k in ("no_shared_folders", "clipboard_disabled",
                              "dragdrop_disabled", "usb_disabled", "nat_without_forwarding", "test_account_only")})

    def test_matching_review_is_accepted(self):
        validate_lease(self.lease, self.facts, self.process, ["test-account"], now=150)

    def test_wrong_guest_user_client_and_environment_rejected(self):
        for field, value in dict(model="Real PC", elevated=True, uuid="host", machine_guid="host-guid",
                                sid="other-user", profile="other-profile", network_drives=1,
                                smb_connections=1, sync_processes=1, client_sha256="tampered").items():
            facts = dict(self.facts, **{field:value})
            with self.subTest(field=field), self.assertRaises(ProbeError):
                validate_lease(self.lease, facts, self.process, ["test-account"], now=150)

    def test_expired_future_or_long_lived_lease_rejected(self):
        for issued, expires in ((1,99),(151,200),(100,1001),(100,float("nan"))):
            lease = dict(self.lease, issued_at=issued, expires_at=expires)
            with self.subTest(times=(issued,expires)), self.assertRaises(ProbeError):
                validate_lease(lease,self.facts,self.process,["test-account"],now=150)

    def test_restart_account_switch_and_shared_resources_rejected(self):
        for process, accounts in ((dict(self.process,pid=13),["test-account"]),
                                  (dict(self.process,started=124),["test-account"]),
                                  (self.process,["other-account"]),
                                  (self.process,["test-account","other-account"])):
            with self.assertRaises(ProbeError):
                validate_lease(self.lease,self.facts,process,accounts,now=150)
        for key in self.lease["host_checks"]:
            lease=copy.deepcopy(self.lease)
            lease["host_checks"][key]=False
            with self.assertRaises(ProbeError):
                validate_lease(lease,self.facts,self.process,["test-account"],now=150)

    def test_revocation_stops_existing_reader(self):
        with patch("wechat_probe.isolation.read_lease",return_value={}):
            with self.assertRaises(ProbeError):
                check_lease_still_active(self.lease)

    def test_privileged_smb_check_must_be_fresh_successful_and_empty(self):
        report=dict(status='checked',query='Get-SmbConnection',checked_at=100,connection_count=0)
        validate_smb_report(report,now=110)
        for change in ({'status':'stopped'},{'query':'Get-SmbMapping'},{'checked_at':80},
                       {'checked_at':120},{'connection_count':1},{'connection_count':None}):
            with self.subTest(change=change), self.assertRaises(ProbeError):
                validate_smb_report(dict(report,**change),now=110)

    def test_redirected_database_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            profile=Path(folder)
            account=profile/'account'
            database=account/'db_storage'
            database.mkdir(parents=True)
            original=Path.stat
            def redirected(path,*args,**kwargs):
                stat=original(path,*args,**kwargs)
                return SimpleNamespace(st_mode=stat.st_mode,st_file_attributes=0x400) if path==database else stat
            with patch.object(Path,'stat',redirected), self.assertRaises(ProbeError):
                check_account_path(account,profile)
