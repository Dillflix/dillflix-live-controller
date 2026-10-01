import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('probe_check',Path(__file__).resolve().parents[1]/'check.py')
check=importlib.util.module_from_spec(spec); spec.loader.exec_module(check)

def healthy():
    return {'schemaVersion':2,'probeBuild':'2.0.0','serviceInstanceId':'750a8eb1-77a4-4ca2-8263-04d5dc5b0e4c',
      'connectionEpoch':1,'connectionStartedAt':{'elapsedRealtimeMs':100},'dumpResponseAt':{'elapsedRealtimeMs':200},
      'dumpTimedOut':False,'dumpFailed':False,'snapshotIsCached':False,'snapshot':{'complete':True,'sessions':[]},
      'collectionHealth':{'listenerConnected':True,'activeSessionsListenerRegistered':True,'callbacksRegistered':True,
        'lastSuccessfulSessionReadElapsedMs':200,'lastPollSuccessElapsedMs':190,'poll':{'lastAttemptSucceeded':True}},
      'journalHealth':{'latestProducedSequence':2,'latestWrittenSequence':2,'initialized':True,'writerAlive':True,
        'writerClosing':False,'lastWriteSucceeded':True,'oldestPendingAgeMs':0,'droppedRecords':0,'writeFailures':0,
        'rotationFailures':0,'oversizedRecords':0,'lossRangeDetailsEvicted':0}}

class CheckTests(unittest.TestCase):
    def test_successfully_read_empty_sessions_are_healthy(self):
        self.assertEqual(check.assess(healthy())['status'],'ready')
    def test_registration_failure_survives_fresh_read(self):
        r=healthy(); r['collectionHealth']['callbacksRegistered']=False
        self.assertIn('callbacksRegistered_false',check.assess(r)['problems'])
    def test_listener_connection_is_not_sufficient(self):
        r=healthy(); r['collectionHealth']['activeSessionsListenerRegistered']=False
        self.assertNotEqual(check.assess(r)['status'],'ready')
    def test_pending_final_sequence_is_not_complete(self):
        r=healthy(); r['journalHealth']['latestProducedSequence']=3
        self.assertIn('journal_pending',check.assess(r)['problems'])
    def test_gap_remains_after_written_high_water_catches_up(self):
        r=healthy(); r['journalHealth']['droppedRecords']=1
        self.assertIn('droppedRecords',check.assess(r)['problems'])
    def test_unchanged_but_stalled_poll_is_not_healthy(self):
        r=healthy(); r['dumpResponseAt']['elapsedRealtimeMs']=10000; r['collectionHealth']['lastSuccessfulSessionReadElapsedMs']=10000
        self.assertIn('lastPollSuccessElapsedMs_stale_or_missing',check.assess(r)['problems'])
    def test_old_epoch_success_does_not_prove_reconnect_poll(self):
        r=healthy(); r['connectionStartedAt']['elapsedRealtimeMs']=195
        self.assertIn('lastPollSuccessElapsedMs_stale_or_missing',check.assess(r)['problems'])
    def test_timed_out_cached_snapshot_not_freshened_by_response_time(self):
        r=healthy(); r['snapshotIsCached']=True; r['dumpTimedOut']=True
        self.assertIn('dump_not_current',check.assess(r)['problems'])
    def test_failed_read_not_treated_as_empty_sessions(self):
        r=healthy(); r['snapshot']['sessions']=None; r['snapshot']['complete']=False
        self.assertIn('snapshot_incomplete',check.assess(r)['problems'])
    def test_legacy_and_missing_v2_fields_do_not_fall_back_to_hash(self):
        self.assertEqual(check.assess({'snapshot':{'sessions':[]}})['status'],'unsupported_schema')
        r=healthy(); del r['serviceInstanceId']
        self.assertEqual(check.assess(r)['status'],'invalid_schema')
    def test_distinct_explicit_ids_allow_same_legacy_hash(self):
        r=healthy(); r['snapshot']['sessions']=[{'sessionInstanceId':i,'sessionToken':'42','readSucceeded':True,'dataComplete':True} for i in ['s1','s2']]
        self.assertEqual(check.assess(r)['status'],'ready')
        r['snapshot']['sessions'][1]['sessionInstanceId']='s1'
        self.assertIn('invalid_session_identity',check.assess(r)['problems'])
    def test_omitted_payload_or_unavailable_writer_never_ready(self):
        r=healthy(); r['journalHealth']['oversizedRecords']=1; r['journalHealth']['writerAlive']=False
        result=check.assess(r)
        self.assertIn('oversizedRecords',result['problems']); self.assertIn('writer_unavailable',result['problems'])

if __name__=='__main__': unittest.main()
