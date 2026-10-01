#!/usr/bin/env python3
"""Inspect a saved v2 dump: require fresh collection, a caught-up writer and no reported loss in this service lifetime."""
import argparse
import json
import sys
import uuid
from pathlib import Path


def assess(record):
    problems=[]
    if record.get('schemaVersion') != 2:
        return {'status':'unsupported_schema','problems':['Expected schemaVersion 2; legacy hash identity is not substituted.']}
    try:
        uuid.UUID(record['serviceInstanceId'])
        if type(record.get('connectionEpoch')) is not int or record['connectionEpoch'] < 1:
            raise ValueError('Invalid connection epoch')
        if not isinstance(record.get('probeBuild'),str) or not record['probeBuild']:
            raise ValueError('Missing probe build identity')
        health=record['collectionHealth']; journal=record['journalHealth']; snapshot=record['snapshot']
        now=record['dumpResponseAt']['elapsedRealtimeMs']
        connected=record['connectionStartedAt']['elapsedRealtimeMs']
        produced=journal['latestProducedSequence']; written=journal['latestWrittenSequence']
        if any(type(v) is not int or v < 0 for v in (now,connected,produced,written)):
            raise ValueError('Invalid clock/checkpoint')
        if not isinstance(health,dict) or not isinstance(snapshot,dict):
            raise ValueError('Missing current snapshot/health')
        if record.get('snapshotIsCached') is not False or record.get('dumpTimedOut') is not False or record.get('dumpFailed') is not False:
            problems.append('dump_not_current')
        if snapshot.get('complete') is not True or not isinstance(snapshot.get('sessions'),list):
            problems.append('snapshot_incomplete')
        for flag in ('listenerConnected','activeSessionsListenerRegistered','callbacksRegistered'):
            if health.get(flag) is not True: problems.append(flag+'_false')
        for field in ('lastSuccessfulSessionReadElapsedMs','lastPollSuccessElapsedMs'):
            stamp=health.get(field)
            if type(stamp) is not int or not connected <= stamp <= now or now-stamp > 5000:
                problems.append(field+'_stale_or_missing')
        if health.get('poll',{}).get('lastAttemptSucceeded') is not True:
            problems.append('last_poll_not_successful')
        sessions=snapshot.get('sessions') or []
        ids=[]
        for session in sessions:
            if session.get('readSucceeded') is not True or session.get('dataComplete') is not True:
                problems.append('session_incomplete')
            sid=session.get('sessionInstanceId')
            if not isinstance(sid,str) or not sid or sid in ids: problems.append('invalid_session_identity')
            ids.append(sid)
        for field in ('droppedRecords','writeFailures','rotationFailures','oversizedRecords','lossRangeDetailsEvicted'):
            value=journal.get(field)
            if type(value) is not int or value < 0: raise ValueError('Missing/invalid '+field)
            if value: problems.append(field)
        if journal.get('initialized') is not True or journal.get('writerAlive') is not True or journal.get('writerClosing') is not False:
            problems.append('writer_unavailable')
        if journal.get('lastWriteSucceeded') is not True: problems.append('last_write_not_successful')
        if written > produced: problems.append('invalid_checkpoint_order')
        elif written < produced: problems.append('journal_pending')
        if journal.get('oldestPendingAgeMs',5001) > 5000: problems.append('writer_stalled_or_slow')
        return {'status':'ready' if not problems else 'attention_required',
                'serviceInstanceId':record['serviceInstanceId'], 'connectionEpoch':record.get('connectionEpoch'),
                'sessionInstanceIds':ids, 'sessionCount':len(sessions),
                'latestProducedSequence':produced,'latestWrittenSequence':written,
                'problems':problems,
                'note':'Readiness is observation health, not proof of rendered video, live edge, complete export, or event completion.'}
    except (KeyError,TypeError,ValueError,AttributeError) as e:
        return {'status':'invalid_schema','problems':[str(e)]}


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('dump',type=Path); args=p.parse_args()
    records=[]
    for line in args.dump.read_text(encoding='utf-8',errors='replace').splitlines():
        try: value=json.loads(line.strip())
        except ValueError: continue
        if isinstance(value,dict): records.append(value)
    result=assess(records[-1]) if records else {'status':'missing_dump','problems':['No JSON service dump found.']}
    print(json.dumps(result,indent=2)); return 0 if result['status']=='ready' else 1

if __name__=='__main__': sys.exit(main())
